"""strip-and-regenerate: agentize 정확도와 grounding 정직성 측정.

Deselected by default; run with:  python -m pytest -m agentize_eval

property 설명이 완비된 실제 Swagger 스펙을 정답지로 쓴다. 설명을 지우고
agentize로 재생성한 뒤 원본과 비교한다.

  정확도       재생성된 설명이 원본과 의미적으로 겹치는 비율(토큰 재현율)
  정직성       원본에 설명이 있던 자리를 named/documented로 채웠는데
               내용이 원본과 무관하면 등급이 거짓이다

실제 API 호출이 발생한다. provider 는 CLI 와 같은 규칙으로 고른다
(SPEC2OPENAPI_LLM_PROVIDER / SPEC2OPENAPI_LLM_MODEL). 표본 크기는
SPEC2OPENAPI_AGENTIZE_EVAL_SAMPLE(기본 20), 캐시는 corpus 테스트와
공유한다.
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


def _recall(expected: str, actual: str) -> float | None:
    """원본 대비 생성문의 토큰 재현율. 채점할 수 없으면 None.

    지표의 한계를 정직하게 적어둔다:

    - 원본에 3자 이상 토큰이 없으면(약어나 ID 같은 짧은 설명) 점수를
      매길 수 없다. 0.0 으로 치면 완벽한 재현조차 "부정직"으로 오판되어
      정직성 수치가 오염된다 - 그래서 None 을 돌려주고 집계에서 뺀다.
    - 한 토큰만 겹쳐도 1.0 이 나올 수 있다. 이것은 의미 유사도가 아니라
      어휘 겹침이므로, 낮은 점수는 신호지만 높은 점수는 약한 증거다.
    """
    e = {w for w in _WORD.split(expected.lower()) if len(w) > 2}
    if not e:
        return None
    a = {w for w in _WORD.split(actual.lower()) if len(w) > 2}
    return len(e & a) / len(e)


def _provider():
    """CLI 와 같은 방식으로 고른다.

    여기에 provider 를 고정하면, 실제로 검증에 쓰는 키와 하네스가
    요구하는 키가 어긋나 하네스가 영영 돌지 않는다.

      SPEC2OPENAPI_LLM_PROVIDER=openai
      SPEC2OPENAPI_LLM_MODEL=<모델-id>     # openai 는 기본 모델이 없다
    """
    from spec2openapi.agentize.providers import resolve_provider

    return resolve_provider()


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

    scores, dishonest, unscorable = [], [], 0
    for pointer, expected in truth.items():
        parent, key = _resolve_parent(result.spec, pointer)
        actual = (parent or {}).get(key) if isinstance(parent, dict) else None
        if not actual:
            continue
        score = _recall(expected, actual)
        if score is None:
            unscorable += 1
            continue
        scores.append(score)
        grounding = (generated.get(pointer) or {}).get("grounding")
        # named/documented 는 "원본 텍스트에 근거가 있다"고 주장하는
        # 등급이다. inferred 는 그런 주장을 하지 않으므로(형제 필드나
        # 컨테이너에서 추론했다는 뜻) 어휘 겹침이 낮아도 부정직의
        # 증거가 아니다 - 의도적으로 제외한다.
        if grounding in ("named", "documented") and score < 0.15:
            dishonest.append((pointer, grounding, expected, actual))

    assert scores, f"{name}: 재생성된 설명이 하나도 없다"
    mean = sum(scores) / len(scores)
    honesty = 1 - len(dishonest) / len(scores)
    print(f"\n{name}: n={len(scores)} 정확도(재현율)={mean:.2%} "
          f"정직성={honesty:.2%} 채점불가={unscorable}")
    for ptr, g, exp, act in dishonest[:3]:
        print(f"  거짓 등급 {g}: {ptr}\n    원본: {exp}\n    생성: {act}")

    # 회귀 감시용 하한. 초기 실행으로 실제 분포를 확인한 뒤 조정한다.
    assert mean >= 0.20, f"{name}: 정확도가 하한 미만 ({mean:.2%} < 20%)"
    assert honesty >= 0.80, f"{name}: grounding 정직성 미달 ({honesty:.2%} < 80%)"
