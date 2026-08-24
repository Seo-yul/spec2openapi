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

from ..openapi import _SAFE_TOOL_RE, _unescape_pointer_token
from .providers import GROUNDING, TRUSTED, Suggestion

#: tool payload에 실리는 문자열이므로 상한을 둔다.
MAX_DESCRIPTION = 400

#: example 은 tool payload 에 그대로 실리므로 상한을 둔다. description 과
#: 달리 자르지 않는다 - JSON 구조를 중간에 자르면 깨진 값이 되어 모델을
#: 오히려 오도한다. 초과하면 거부하고 기록한다.
MAX_EXAMPLE = 400

#: 거부 메시지에 넣을 포인터 길이 상한. 메시지는 운영자가 터미널에서
#: 읽고, 포인터는 신뢰할 수 없는 입력에서 온다.
_PTR_IN_MESSAGE = 120

#: ANSI CSI 시퀀스 전체(ESC 도입부 + 파라미터 + 종결 문자). ESC 만
#: 지우면 "[2J" 같은 잔재가 설명에 남는다. 리터럴 ESC 접두가 필수라
#: "array[2]" 나 "[0-9;]*" 같은 정상 텍스트에는 매치되지 않는다.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

#: 남은 제어문자. 탭(\t)과 개행(\n)은 설명에서 의미가 있으므로 남긴다.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_METHOD = "(?:get|put|post|delete|patch|options|head)"

#: 스키마 노드를 가리키는 형태. example 쓰기 게이트에도 재사용한다.
_SCHEMA_TARGET = re.compile(
    r"^#/components/schemas/[^/]+"
    r"(?:/properties/[^/]+|/items)*"
    r"/(?:description|example)\Z")

#: 쓰기가 허용되는 포인터 형태. 이 목록이 안전의 1차 방어선이다.
_ALLOWED = (
    _SCHEMA_TARGET,
    re.compile(rf"^#/paths/[^/]+/{_METHOD}/(?:summary|description)\Z"),
    re.compile(rf"^#/paths/[^/]+/{_METHOD}/parameters/[0-9]+/description\Z"),
)

#: --rename-tools 를 켰을 때만 열리는 추가 형태.
_ALLOWED_RENAME = re.compile(rf"^#/paths/[^/]+/{_METHOD}/operationId\Z")


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


def _safe(pointer: str) -> str:
    """거부 메시지용: 길이를 자르고 repr 로 제어문자를 이스케이프한다."""
    return repr(pointer[:_PTR_IN_MESSAGE])


def _clean(text: str) -> str:
    """모델이 쓴 문자열에서 터미널 제어 시퀀스와 제어문자를 제거한다.

    이 문자열은 스펙 문서와 tool payload 에 그대로 실리고, 운영자가
    터미널에서 읽는다. CSI 를 먼저 통째로 지우고 남은 제어문자를
    지운다 - 순서가 뒤바뀌면 ESC 가 먼저 사라져 잔재가 남는다.
    """
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", text))


def _clean_value(value: Any) -> Any:
    """example 값 안의 문자열을 재귀적으로 정리한다.

    example 은 스칼라일 수도 객체·배열일 수도 있다. 어느 쪽이든 그 안의
    문자열은 description 과 똑같이 스펙 문서와 tool payload 에 실리므로
    같은 정리를 받아야 한다.
    """
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        return {k: _clean_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_value(v) for v in value]
    return value


def apply_suggestions(spec: dict, suggestions: Iterable[Suggestion], *,
                      allow_speculative: bool = False,
                      rename_tools: bool = False) -> tuple[dict, ApplyReport]:
    """제안을 적용한 새 스펙과 보고서를 돌려준다. 입력은 변경하지 않는다."""
    out = copy.deepcopy(spec)
    report = ApplyReport()

    for sug in suggestions:
        ptr = sug.pointer
        # 포인터 검사가 grounding 검사보다 먼저다: 화이트리스트 밖
        # 시도는 speculative 여도 기록되어야 프롬프트 인젝션 신호가
        # 보존된다.
        if not _pointer_allowed(ptr, rename_tools=rename_tools):
            report.rejected.append(f"{_safe(ptr)}: 허용되지 않는 위치")
            continue
        if sug.grounding not in GROUNDING:
            report.rejected.append(
                f"{_safe(ptr)}: 알 수 없는 grounding {sug.grounding!r}")
            continue
        if sug.grounding not in TRUSTED and not allow_speculative:
            report.dropped_speculative += 1
            continue
        if not isinstance(sug.description, str):
            report.rejected.append(f"{_safe(ptr)}: 문자열이 아닌 값")
            continue
        text = _clean(sug.description).strip()
        if not text:
            report.rejected.append(f"{_safe(ptr)}: 빈 문자열")
            continue

        parent, key = _resolve_parent(out, ptr)
        if parent is None:
            report.rejected.append(f"{_safe(ptr)}: 존재하지 않는 경로")
            continue
        if not isinstance(parent, dict):
            report.rejected.append(f"{_safe(ptr)}: 매핑이 아닌 부모")
            continue

        if key == "operationId":
            if not _SAFE_TOOL_RE.fullmatch(text):
                report.rejected.append(
                    f"{_safe(ptr)}: operationId 가 tool 이름 규칙"
                    f"([A-Za-z0-9_.-], 1~64자)에 맞지 않음")
                continue
            prior = parent.get("operationId")
            if isinstance(prior, str) and prior:
                report.renamed[ptr.rsplit("/", 1)[0]] = prior
            parent[key] = text
        elif key == "example":
            if not _example_ok(text):
                report.rejected.append(
                    f"{_safe(ptr)}: example 이 직렬화 불가이거나 "
                    f"{MAX_EXAMPLE}자를 넘음")
                continue
            parent[key] = text
        else:
            parent[key] = _truncate(text)
        report.applied[ptr] = sug.grounding

        # 예시는 설명이 붙은 스키마 노드에만 쓴다. key 이름이 아니라
        # 포인터가 스키마를 가리키는지로 게이트해야 operation 에
        # example 이 붙는 것을 막을 수 있다.
        if (sug.example is not None and key == "description"
                and _SCHEMA_TARGET.match(ptr)):
            cleaned = _clean_value(copy.deepcopy(sug.example))
            if _example_ok(cleaned):
                parent.setdefault("example", cleaned)
            else:
                report.rejected.append(
                    f"{_safe(ptr)}: example 이 직렬화 불가이거나 "
                    f"{MAX_EXAMPLE}자를 넘음")

    return out, report


def can_record(spec: dict) -> bool:
    """root x-s2o에 기록할 수 있는가.

    기록할 수 없으면 보강을 수행하면 안 된다 - 재실행이 중복 생성을
    일으킨다. minify_for_mcp가 같은 이유로 enrichment를 건너뛴다.
    """
    root = spec.get("x-s2o")
    return root is None or isinstance(root, dict)


def already_generated(spec: dict) -> set[str]:
    """이전 실행이 채운 포인터 집합. 재실행 시 건너뛸 대상이다."""
    root = spec.get("x-s2o")
    if not isinstance(root, dict):
        return set()
    block = root.get("agentize")
    if not isinstance(block, dict):
        return set()
    generated = block.get("generated")
    return set(generated) if isinstance(generated, dict) else set()


def record_provenance(spec: dict, report: ApplyReport, *, provider: str,
                      model: str, targets: Iterable[str]) -> None:
    """x-s2o.agentize에 기록한다. 스펙을 제자리 수정한다.

    스키마 노드 안에는 아무것도 쓰지 않는다: 스키마 레벨 확장은 tool
    payload에 그대로 복사되므로, 거기에 provenance를 넣으면 이 기능이
    줄이려는 노이즈를 오히려 늘리게 된다.
    """
    if not can_record(spec):
        return
    root = spec.setdefault("x-s2o", {})
    prior = root.get("agentize")
    prior = prior if isinstance(prior, dict) else {}
    generated = dict(prior.get("generated") or {})
    generated.update({p: {"grounding": g} for p, g in report.applied.items()})
    renamed = dict(prior.get("renamed") or {})
    renamed.update({p: {"from": old} for p, old in report.renamed.items()})

    block = {
        "provider": provider,
        "model": model,
        "targets": list(targets),
        "generated": generated,
        "dropped_speculative": (int(prior.get("dropped_speculative") or 0)
                                + report.dropped_speculative),
    }
    if renamed:
        block["renamed"] = renamed
    root["agentize"] = block
