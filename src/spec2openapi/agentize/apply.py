"""모델이 반환한 문자열을 스펙에 쓰는 유일한 통로.

안전 모델은 두 겹이다.

1. 포인터 화이트리스트 - 정규식으로 허용된 형태만 통과한다. x-soap,
   type, required, $ref 는 어떤 응답으로도 도달할 수 없다.
2. grounding 게이트 - speculative 는 기본 폐기한다.

openapi.resolve_pointer()는 dict 순회만 하므로(parameters/0 같은 리스트
인덱스를 다루지 않는다) 여기서는 자체 해석기를 쓴다.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..openapi import _unescape_pointer_token
from .providers import GROUNDING, TRUSTED, Suggestion

#: tool payload에 실리는 문자열이므로 상한을 둔다.
MAX_DESCRIPTION = 400

#: example 은 tool payload 에 그대로 실리므로 상한을 둔다. description 과
#: 달리 자르지 않는다 - JSON 구조를 중간에 자르면 깨진 값이 되어 모델을
#: 오히려 오도한다. 초과하면 거부하고 기록한다.
MAX_EXAMPLE = 400

_METHOD = "(?:get|put|post|delete|patch|options|head)"

#: 쓰기가 허용되는 포인터 형태. 이 목록이 안전의 1차 방어선이다.
_ALLOWED = (
    # 스키마(중첩 properties/items 포함)의 description / example
    re.compile(r"^#/components/schemas/[^/]+"
               r"(?:/properties/[^/]+|/items)*"
               r"/(?:description|example)$"),
    # 스키마 루트의 description
    re.compile(r"^#/components/schemas/[^/]+/description$"),
    # operation 의 summary / description
    rf"^#/paths/[^/]+/{_METHOD}/(?:summary|description)$",
    # operation 의 parameters[i].description
    rf"^#/paths/[^/]+/{_METHOD}/parameters/\d+/description$",
)
_ALLOWED = tuple(re.compile(p) if isinstance(p, str) else p for p in _ALLOWED)

#: --rename-tools 를 켰을 때만 열리는 추가 형태.
_ALLOWED_RENAME = re.compile(rf"^#/paths/[^/]+/{_METHOD}/operationId$")


@dataclass
class ApplyReport:
    """무엇이 적용되고 무엇이 거부됐는지."""

    applied: dict[str, str] = field(default_factory=dict)
    dropped_speculative: int = 0
    rejected: list[str] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)


def _split(pointer: str) -> list[str]:
    body = pointer[1:] if pointer.startswith("#") else pointer
    return [_unescape_pointer_token(t) for t in body.split("/") if t != ""]


def _resolve_parent(spec: dict, pointer: str):
    """(부모 컨테이너, 마지막 토큰)을 돌려준다. 못 찾으면 (None, None).

    노드를 새로 만들지 않는다 - 존재하지 않는 경로는 거부 대상이다.
    """
    toks = _split(pointer)
    if not toks:
        return None, None
    node: Any = spec
    for tok in toks[:-1]:
        if isinstance(node, dict):
            node = node.get(tok)
        elif isinstance(node, list) and tok.isdigit():
            idx = int(tok)
            node = node[idx] if idx < len(node) else None
        else:
            return None, None
        if node is None:
            return None, None
    return node, toks[-1]


def _pointer_allowed(pointer: str, *, rename_tools: bool) -> bool:
    if any(rx.match(pointer) for rx in _ALLOWED):
        return True
    return rename_tools and bool(_ALLOWED_RENAME.match(pointer))


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_DESCRIPTION:
        return text
    return text[:MAX_DESCRIPTION - 1].rstrip() + "…"


def _example_ok(value: Any) -> bool:
    """JSON 직렬화 가능하고 상한 안에 드는 값인가."""
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return len(rendered) <= MAX_EXAMPLE


def apply_suggestions(spec: dict, suggestions: Iterable[Suggestion], *,
                      allow_speculative: bool = False,
                      rename_tools: bool = False) -> tuple[dict, ApplyReport]:
    """제안을 적용한 새 스펙과 보고서를 돌려준다. 입력은 변경하지 않는다."""
    out = copy.deepcopy(spec)
    report = ApplyReport()

    for sug in suggestions:
        ptr = sug.pointer
        if sug.grounding not in GROUNDING:
            report.rejected.append(f"{ptr}: 알 수 없는 grounding "
                                   f"{sug.grounding!r}")
            continue
        if sug.grounding not in TRUSTED and not allow_speculative:
            report.dropped_speculative += 1
            continue
        if not _pointer_allowed(ptr, rename_tools=rename_tools):
            report.rejected.append(f"{ptr}: 허용되지 않는 위치")
            continue
        if not isinstance(sug.description, str) or not sug.description.strip():
            report.rejected.append(f"{ptr}: 문자열이 아닌 값")
            continue

        parent, key = _resolve_parent(out, ptr)
        if parent is None:
            report.rejected.append(f"{ptr}: 존재하지 않는 경로")
            continue
        if not isinstance(parent, dict):
            report.rejected.append(f"{ptr}: 매핑이 아닌 부모")
            continue

        if key == "operationId":
            report.renamed[ptr.rsplit("/", 1)[0]] = parent.get("operationId")
            parent["operationId"] = sug.description.strip()
        else:
            parent[key] = _truncate(sug.description)
        report.applied[ptr] = sug.grounding

        if sug.example is not None and key == "description":
            # 예시는 설명이 붙은 바로 그 스키마 노드에만 쓴다.
            if _example_ok(sug.example):
                parent.setdefault("example", sug.example)
            else:
                report.rejected.append(
                    f"{ptr}: example 이 직렬화 불가이거나 "
                    f"{MAX_EXAMPLE}자를 넘음")

    return out, report
