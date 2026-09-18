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
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..minify import _fold_key, _split_folds
from ..openapi import (
    _HTTP_METHODS,
    _SAFE_TOOL_RE,
    _unescape_pointer_token,
)
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

# openapi.py 의 정본에서 파생한다. 손으로 베끼면 drift 가 생기고,
# 실제로 trace 가 빠져 있어 trace operation 의 제안이 전부 거부됐다.
_METHOD = f"(?:{'|'.join(sorted(_HTTP_METHODS))})"

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
        # NaN/Infinity 는 어떤 RFC 8259 파서도 읽지 못하는 토큰이 된다
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return len(rendered) <= MAX_EXAMPLE


#: example 값의 declared type 검사를 건너뛰는 type. 없음/"string"이면
#: example이 property의 선언된 type과 모순될 수 없다.
_UNCHECKED_EXAMPLE_TYPES = (None, "string")

_NUMERIC_EXAMPLE_TYPES = ("integer", "number")

#: 모델의 example 은 응답 스키마상 언제나 문자열이다. 이 type 들에는
#: JSON 으로 파싱해 모양이 맞을 때만 쓴다.
_STRUCTURED_EXAMPLE_TYPES = {"array": list, "object": dict}


def _typed_example(raw: Any, declared_type: Any) -> tuple[bool, Any]:
    """example 값을 부모 스키마의 선언된 type과 맞춰 검사한다 (Ruling 53).

    property가 "type": "integer"인데 model이 "twelve" 같은 문자열을
    example으로 주면, FastMCP는 그 example을 tool payload에 그대로
    복사한다 - agent는 정수 필드에 문자열 example을 보고 잘못 조립한다.
    verify()도 openapi-spec-validator도 example의 type을 검사하지
    않으므로, 여기가 유일한 방어선이다.

    반환: (허용 여부, 쓸 값).

    - type이 없거나 "string"이면 그대로 통과시킨다 (모순 가능성이 없다).
    - "boolean"이면 문자열 "true"/"false"(대소문자 무관)만 통과시키고
      실제 bool로 파싱해 돌려준다.
    - "integer"/"number"면 문자열이 그 type으로 파싱될 때만 통과시키고
      파싱된 값을 돌려준다.
    - "array"/"object"면 문자열을 JSON으로 파싱해 모양(list/dict)이 맞을
      때만 통과시키고 파싱된 값을 돌려준다. 모델의 example은 언제나
      문자열이라, 파싱하지 않으면 배열·객체 필드에 문자열이 붙는다.
    """
    if isinstance(declared_type, list):
        # 3.1 의 nullable 스칼라는 ["integer", "null"] 로 온다.
        # 리스트라고 통과시키면 이 함수가 막으라고 있는 바로 그
        # 모순을 3.1 스펙에서만 놓친다.
        declared_type = next(
            (t for t in declared_type if t != "null"), None)
    if not isinstance(declared_type, str) or declared_type in _UNCHECKED_EXAMPLE_TYPES:
        return True, raw
    shape = _STRUCTURED_EXAMPLE_TYPES.get(declared_type)
    if shape is not None:
        value = raw
        if isinstance(raw, str):
            if len(raw) > MAX_EXAMPLE:
                # 상한을 넘는 원문은 파싱하지 않는다 - 두 호출부 모두 뒤이어
                # _example_ok 로 크기를 검사해 거부한다. 파싱 뒤의 정리는
                # 재귀이고 3.12+ 의 디코더는 재귀 한도보다 깊은 중첩도
                # 파싱하므로, 상한 안의 원문만 다뤄야 깊이가 한도에 닿지 않는다.
                return True, raw
            try:
                value = json.loads(raw)
            except (ValueError, RecursionError):
                return False, None
            # 정리는 파싱 전 원문에 적용됐다 - JSON 이스케이프는 파싱 뒤에야
            # 실제 제어문자가 되므로 파싱한 값을 다시 정리한다.
            value = _clean_value(value)
        return (True, value) if isinstance(value, shape) else (False, None)
    if declared_type == "boolean":
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return True, raw.strip().lower() == "true"
        return False, None
    if declared_type in _NUMERIC_EXAMPLE_TYPES:
        if not isinstance(raw, str):
            return False, None
        try:
            parsed = (int(raw.strip()) if declared_type == "integer"
                      else float(raw.strip()))
        except ValueError:
            return False, None
        if not math.isfinite(parsed):
            # float("NaN"/"Infinity") 는 파싱에 성공하지만 JSON 으로
            # 직렬화하면 어떤 RFC 8259 파서도 읽지 못하는 토큰이 된다.
            return False, None
        return True, parsed
    return True, raw


#: 다른 스키마에서 type 을 가져오는 합성 키워드. 변환기는 형제 키가 있는
#: $ref 를 allOf 로 감싼다.
_COMPOSITION_KEYS = ("allOf", "oneOf", "anyOf")


def _type_from_elsewhere(parent: dict) -> bool:
    """type 을 $ref 나 type 선언 없는 합성에서 가져오는 필드인가."""
    return "$ref" in parent or ("type" not in parent and any(
        k in parent for k in _COMPOSITION_KEYS))


def _example_for(parent: dict, raw: Any) -> tuple[bool, Any]:
    """부모 스키마에 맞춘 example. type 을 다른 스키마에서 가져오는 필드는
    type 을 확인할 수 없어 받지 않는다."""
    if _type_from_elsewhere(parent):
        return False, None
    return _typed_example(raw, parent.get("type"))


def _example_rejection(parent: dict) -> str:
    if _type_from_elsewhere(parent):
        return "$ref·합성 필드에는 example 을 쓰지 않음"
    return "example 이 선언된 type과 맞지 않음"


#: operation 의 description/operationId 포인터. minify 의 fold 기록과
#: 맞물리는 필드다.
_OP_FIELD = re.compile(
    rf"^#/paths/([^/]+)/({_METHOD})/(?:description|operationId)\Z")


def _fold_record(spec: dict) -> dict | None:
    s2o = spec.get("x-s2o")
    minify = s2o.get("minify") if isinstance(s2o, dict) else None
    folded = minify.get("folded") if isinstance(minify, dict) else None
    return folded if isinstance(folded, dict) else None


def _op_fold_key(pointer: str, op: dict) -> str | None:
    """operation 필드 포인터면 minify 의 fold 기록 키, 아니면 None."""
    m = _OP_FIELD.match(pointer)
    if m is None:
        return None
    return _fold_key(_unescape_pointer_token(m.group(1)), m.group(2), op)


def _kept_folds(spec: dict, key: str | None, op: dict) -> str:
    """operation 설명을 새로 쓸 때 이어 붙일, minify 가 접어 넣은 줄.

    새 설명이 이 줄까지 덮으면 folded 기록만 남아 다시 돌린 minify 도
    복원하지 못한다. 기록에 있는 operation 만 벗긴다 - 사용자가 쓴
    marker 모양 줄을 우리 것으로 오인하지 않기 위해서다. key 는 이
    실행에서 개명되기 전의 기록 키다.
    """
    folded = _fold_record(spec)
    desc = op.get("description")
    if folded is None or key not in folded or not isinstance(desc, str):
        return ""
    return "".join("\n" + line for line in _split_folds(desc)[1])


def _move_fold_keys(spec: dict, moves: dict[str, str]) -> None:
    """개명한 operation 의 fold 기록을 새 이름으로 옮긴다.

    옛 이름에 남으면 다음 minify 가 같은 줄을 한 번 더 접어 넣는다.
    원래 키 기준으로 한 번에 옮긴다 - 하나씩 옮기면 a->b, b->c 같은
    이어진 개명이나 맞바꾸기에서 아직 개명 전인 operation 의 기록을
    덮어쓴다.
    """
    folded = _fold_record(spec)
    if not folded or not moves:
        return
    moved = {moves.get(k, k): v for k, v in folded.items()}
    folded.clear()
    folded.update(moved)


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
        return {(_clean(k) if isinstance(k, str) else k): _clean_value(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_value(v) for v in value]
    return value


def apply_suggestions(spec: dict, suggestions: Iterable[Suggestion], *,
                      allow_speculative: bool = False,
                      rename_tools: bool = False) -> tuple[dict, ApplyReport]:
    """제안을 적용한 새 스펙과 보고서를 돌려준다. 입력은 변경하지 않는다."""
    out = copy.deepcopy(spec)
    report = ApplyReport()
    # operation 별 이 실행 이전의 fold 기록 키, 그리고 개명으로 옮길 키.
    fold_keys: dict[int, str | None] = {}
    fold_moves: dict[str, str] = {}

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
            if prior == text:
                # 같은 이름을 돌려준 것은 개명이 아니다. 기록하면 applied 를
                # 부풀리고 "N 개를 개명했다"는 거짓 보고가 된다.
                continue
            if isinstance(prior, str) and prior:
                report.renamed[ptr.rsplit("/", 1)[0]] = prior
            old_key = fold_keys.setdefault(id(parent),
                                           _op_fold_key(ptr, parent))
            parent[key] = text
            new_key = _op_fold_key(ptr, parent)
            if old_key is not None and new_key is not None:
                fold_moves[old_key] = new_key
        elif key == "example":
            ok_type, typed = _example_for(parent, text)
            if not ok_type:
                report.rejected.append(
                    f"{_safe(ptr)}: {_example_rejection(parent)}")
                continue
            if not _example_ok(typed):
                report.rejected.append(
                    f"{_safe(ptr)}: example 이 직렬화 불가이거나 "
                    f"{MAX_EXAMPLE}자를 넘음")
                continue
            parent[key] = typed
        elif key == "description":
            fold_key = fold_keys.get(id(parent)) or _op_fold_key(ptr, parent)
            folds = _kept_folds(out, fold_key, parent)
            if folds:
                # 모델은 접힌 줄이 붙은 원래 설명을 보고 쓴다 - 그 줄을
                # 되풀이해도 기록된 한 벌만 남긴다.
                text = _split_folds(text)[0].strip()
                if not text:
                    report.rejected.append(
                        f"{_safe(ptr)}: 접힌 줄 외에 설명이 없음")
                    continue
            parent[key] = _truncate(text) + folds
        else:
            parent[key] = _truncate(text)
        report.applied[ptr] = sug.grounding

        # 예시는 설명이 붙은 스키마 노드에만 쓴다. key 이름이 아니라
        # 포인터가 스키마를 가리키는지로 게이트해야 operation 에
        # example 이 붙는 것을 막을 수 있다.
        if (sug.example is not None and key == "description"
                and _SCHEMA_TARGET.match(ptr)):
            cleaned = _clean_value(copy.deepcopy(sug.example))
            ok_type, typed = _example_for(parent, cleaned)
            if not ok_type:
                report.rejected.append(
                    f"{_safe(ptr)}: {_example_rejection(parent)}")
            elif _example_ok(typed):
                parent.setdefault("example", typed)
            else:
                report.rejected.append(
                    f"{_safe(ptr)}: example 이 직렬화 불가이거나 "
                    f"{MAX_EXAMPLE}자를 넘음")

    _move_fold_keys(out, fold_moves)
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

    generated의 각 항목은 그것을 쓴 provider/model을 함께 담는다
    (Ruling 64) - 서로 다른 provider/model로 재실행하면 최상위
    provider/model은 마지막 실행 값으로 덮이지만, 개별 포인터는 자신을
    쓴 provider/model을 그대로 유지해야 "어느 문장을 어느 모델이
    썼는가"를 사후에 구분할 수 있다.
    """
    if not can_record(spec):
        return
    root = spec.setdefault("x-s2o", {})
    prior = root.get("agentize")
    prior = prior if isinstance(prior, dict) else {}
    generated = dict(prior.get("generated") or {})
    generated.update({p: {"grounding": g, "provider": provider, "model": model}
                      for p, g in report.applied.items()})
    renamed = dict(prior.get("renamed") or {})
    renamed.update({p: {"from": old} for p, old in report.renamed.items()})

    block = {
        "provider": provider,
        "model": model,
        "targets": list(targets),
        "generated": generated,
        # 이전 실행의 누적이 아니라 이번 실행에서 실제로 폐기한 값이다
        # (Ruling 60) - 누적이면 재실행할 때마다 실제 상태와 어긋난 수가
        # 남는다.
        "dropped_speculative": report.dropped_speculative,
    }
    if renamed:
        block["renamed"] = renamed
    root["agentize"] = block
