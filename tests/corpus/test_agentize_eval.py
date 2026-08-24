"""strip-and-regenerate: agentize 정확도와 grounding 정직성 측정.

Deselected by default; run with:  python -m pytest -m agentize_eval

property 설명이 완비된 실제 Swagger 스펙을 정답지로 쓴다. 설명을 지우고
agentize로 재생성한 뒤 원본과 비교한다.

  정확도       재생성된 설명이 원본과 의미적으로 겹치는 비율(토큰 재현율)
  정직성       원본에 설명이 있던 자리를 named/documented로 채웠는데
               내용이 원본과 무관하면 등급이 거짓이다

실제 API 호출이 발생한다. 표본 크기: SPEC2OPENAPI_AGENTIZE_EVAL_SAMPLE
(기본 20), 캐시는 corpus 테스트와 공유한다.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.agentize_eval

CACHE = Path(os.environ.get(
    "SPEC2OPENAPI_CORPUS_CACHE",
    Path.home() / ".cache" / "spec2openapi-corpus"))
SAMPLE = int(os.environ.get("SPEC2OPENAPI_AGENTIZE_EVAL_SAMPLE", "20"))
_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _fully_documented_specs() -> list[tuple[str, dict]]:
    """property 설명이 100%인 Swagger 2.0 스펙 = 정답지."""
    out = []
    for f in sorted(CACHE.glob("*.json")):
        if f.name == "list.json":
            continue
        try:
            s = json.loads(f.read_text())
        except Exception:
            continue
        if not str(s.get("swagger", "")).startswith("2"):
            continue
        total = documented = 0
        for _, sch in (s.get("definitions") or {}).items():
            for _, pv in ((sch or {}).get("properties") or {}).items():
                if isinstance(pv, dict):
                    total += 1
                    documented += bool(pv.get("description"))
        if total >= 8 and total == documented:
            out.append((f.stem, s))
        if len(out) >= SAMPLE:
            break
    return out


def _strip_property_descriptions(spec: dict) -> dict[str, str]:
    """설명을 지우고 정답을 포인터별로 돌려준다."""
    from spec2openapi.agentize.targets import escape_token

    truth = {}
    for sname, sch in (spec.get("components", {}).get("schemas") or {}).items():
        for pname, pv in ((sch or {}).get("properties") or {}).items():
            if isinstance(pv, dict) and pv.get("description"):
                key = (f"#/components/schemas/{escape_token(sname)}"
                       f"/properties/{escape_token(pname)}/description")
                truth[key] = pv.pop("description")
    return truth


def _recall(expected: str, actual: str) -> float:
    e = {w for w in _WORD.split(expected.lower()) if len(w) > 2}
    a = {w for w in _WORD.split(actual.lower()) if len(w) > 2}
    return len(e & a) / len(e) if e else 0.0


def _provider():
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    return AnthropicProvider()


@pytest.mark.parametrize("name,swagger", _fully_documented_specs(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_strip_and_regenerate(name, swagger):
    from spec2openapi.agentize import agentize_spec
    from spec2openapi.agentize.apply import _resolve_parent
    from spec2openapi.swagger import convert_swagger

    spec = convert_swagger(swagger)
    truth = _strip_property_descriptions(spec)
    if not truth:
        pytest.skip(f"{name}: 정답 대상이 없다")

    result = agentize_spec(copy.deepcopy(spec), _provider(),
                           kinds=("properties",))
    generated = result.spec.get("x-s2o", {}).get("agentize", {}).get(
        "generated", {})

    scores, dishonest = [], []
    for pointer, expected in truth.items():
        parent, key = _resolve_parent(result.spec, pointer)
        actual = (parent or {}).get(key) if isinstance(parent, dict) else None
        if not actual:
            continue
        score = _recall(expected, actual)
        scores.append(score)
        grounding = (generated.get(pointer) or {}).get("grounding")
        # 원본에 설명이 있던 자리다. 근거를 named/documented로 주장했는데
        # 내용이 전혀 겹치지 않으면 등급이 거짓이다.
        if grounding in ("named", "documented") and score < 0.15:
            dishonest.append((pointer, grounding, expected, actual))

    assert scores, f"{name}: 재생성된 설명이 하나도 없다"
    mean = sum(scores) / len(scores)
    honesty = 1 - len(dishonest) / len(scores)
    print(f"\n{name}: n={len(scores)} 정확도(재현율)={mean:.2%} "
          f"정직성={honesty:.2%}")
    for ptr, g, exp, act in dishonest[:3]:
        print(f"  거짓 등급 {g}: {ptr}\n    원본: {exp}\n    생성: {act}")

    # 회귀 감시용 하한. 초기 실행으로 실제 분포를 확인한 뒤 조정한다.
    assert mean >= 0.20, f"{name}: 정확도가 하한 미만 ({mean:.2%})"
    assert honesty >= 0.80, f"{name}: grounding 정직성 미달 ({honesty:.2%})"
