"""무엇을 보강 대상으로 볼 것인가 - 결정론적 판정.

LLM은 이 모듈에 관여하지 않는다. "비어 있는가"가 아니라 "agent에게
쓸모가 있는가"로 판정하되, 판정 자체는 전부 코드가 한다.

주의: FastMCP는 operation에 `description`이 없으면 `summary`를 tool
설명으로 서빙한다(minify.py의 max_description 주석 참조). 따라서
summary가 충실하고 description만 비어 있는 operation은 이미 충분하며
대상이 아니다.

한계: property 순회는 스키마의 최상위 properties만 본다 (중첩 재귀
없음) - 뒤 단계(pass 2a)가 모델이 돌려준 필드 이름으로 포인터를
재구성하므로 중첩 필드는 잘못된 위치에 쓰이게 된다. 실제 Swagger
140개 스펙 실측에서 최상위 property가 27,121개(95.1%)로 대다수이며,
공유 형태는 대개 $ref로 분리되어 각자 최상위로 순회되므로 이 한계의
실질 영향은 작다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..checks import _component_schemas
from ..minify import _split_folds
from ..openapi import _operations

#: 이보다 짧은 설명은 정보량이 없다고 본다.
MIN_DESCRIPTION = 12

#: --target 이 받는 보강 종류. 나열 순서가 우선순위다.
#:
#: "examples"는 find_targets()가 만들어내는 대상 kind가 아니다 - 이미
#: properties 처리의 부산물로 나오는 example 값을 적용할지 말지 결정하는
#: permission이다 (agentize_spec()이 kinds에 "examples"가 있을 때만 그
#: 값을 쓴다). "enums"는 아예 생산자가 없다 - enum 의미는 property
#: description에 문장으로 들어가므로 "properties" kind가 담당한다.
KINDS = ("properties", "desc", "params", "examples")

_NORM_RE = re.compile(r"[^a-z0-9]")


@dataclass(frozen=True)
class Target:
    """보강 대상 한 곳.

    pointer: RFC 6901 JSON Pointer ('#'으로 시작)
    kind   : KINDS 중 하나
    reason : empty | duplicate | restatement | too-short | overwrite
    name   : 프롬프트에 넘길 사람이 읽는 이름 (필드명 / operationId)
    """

    pointer: str
    kind: str
    reason: str
    name: str


def normalize(s: str | None) -> str:
    """비교용 정규화: 소문자화 후 영숫자만 남긴다."""
    return _NORM_RE.sub("", (s or "").lower())


def escape_token(tok: str) -> str:
    """RFC 6901 포인터 토큰 이스케이프 (~ -> ~0, / -> ~1). 순서 중요."""
    return str(tok).replace("~", "~0").replace("/", "~1")


def low_value_reason(description: Any, *, name: str | None = None,
                     sibling: str | None = None) -> str | None:
    """agent에게 쓸모없는 설명이면 그 사유를, 쓸모 있으면 None을 돌려준다."""
    text = "" if description is None else str(description).strip()
    if not text:
        return "empty"
    norm = normalize(text)
    if sibling and norm == normalize(sibling):
        return "duplicate"
    if name and norm == normalize(name):
        return "restatement"
    if len(text) < MIN_DESCRIPTION:
        return "too-short"
    return None


def find_targets(spec: dict, *, kinds: Iterable[str] = KINDS,
                 policy: str = "default") -> list[Target]:
    """보강이 필요한 위치를 순서대로 돌려준다.

    policy: "default"    빈 곳 + 저품질 판정된 곳
            "empty-only" 빈 곳만
            "overwrite"  전부
    """
    kinds = tuple(kinds)
    out: list[Target] = []
    # minify_for_mcp가 무엇을 접었는지의 기록. 없으면 아무것도 벗기지 않는다.
    root = spec.get("x-s2o") if isinstance(spec, dict) else None
    minify_rec = root.get("minify") if isinstance(root, dict) else None
    folded = minify_rec.get("folded") if isinstance(minify_rec, dict) else None
    _folded_ops = set(folded) if isinstance(folded, dict) else set()

    def add(pointer, kind, name, description, sibling=None):
        if kind not in kinds:
            return
        reason = low_value_reason(description, name=name, sibling=sibling)
        if policy == "overwrite":
            reason = reason or "overwrite"
        elif policy == "empty-only" and reason != "empty":
            reason = None
        if reason:
            out.append(Target(pointer, kind, reason, name))

    schemas = _component_schemas(spec) if isinstance(spec, dict) else {}
    for sname, schema in (schemas or {}).items():
        props = (schema or {}).get("properties")
        if not isinstance(props, dict):
            continue
        prefix = f"#/components/schemas/{escape_token(sname)}"
        for pname, pv in props.items():
            if not isinstance(pv, dict):
                continue
            add(f"{prefix}/properties/{escape_token(pname)}/description",
                "properties", pname, pv.get("description"))

    for path, method, op in _operations(spec):
        base = f"#/paths/{escape_token(path)}/{method}"
        # FastMCP는 description이 없으면 summary를 서빙한다: 실제로
        # 모델에게 도달하는 텍스트를 기준으로 판정한다.
        #
        # minify_for_mcp(enrich=...)가 접어 넣은 "Errors: ..." / "Example
        # ...: ..." 줄은 "이 operation이 무엇을 하는가"에 대한 답이 아니라
        # 부수 메타데이터다. 그것까지 설명으로 세면 설명이 전혀 없는
        # operation이 이미 문서화된 것처럼 보여 대상에서 빠진다.
        # folded 기록이 있을 때만 벗긴다 - 사용자가 직접 쓴 marker 모양
        # 줄을 우리 것으로 오인하지 않기 위해서다(_split_folds 주석 참조).
        desc, summary = op.get("description"), op.get("summary")
        if desc and _folded_ops and op.get("operationId") in _folded_ops:
            desc = _split_folds(desc)[0].strip() or None
        effective = desc if desc else summary
        add(f"{base}/description", "desc", op.get("operationId") or "",
            effective, sibling=summary if desc else None)
        for i, prm in enumerate(op.get("parameters") or []):
            if isinstance(prm, dict) and "$ref" not in prm:
                add(f"{base}/parameters/{i}/description", "params",
                    prm.get("name") or "", prm.get("description"))
    return out
