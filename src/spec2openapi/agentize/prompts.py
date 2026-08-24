"""LLM에게 보낼 입력의 압축과 프롬프트 조립.

digest()는 화이트리스트로 동작한다. 블랙리스트로 만들면 나중에 새
확장이 추가됐을 때 조용히 새어 나간다.

여기서 깎는 것은 *LLM에게 보내는 입력*이며, 출력 스펙과는 무관하다.
출력 스펙의 x-soap/xml은 SOAP 브리지가 런타임에 읽으므로 유지된다.

하위 스키마 분류는 minify.py의 _SUBSCHEMA_* 패턴을 따른다.
pass 2b는 $ref를 인라인하지 않는다 - pass 2a가 공유 스키마를 처리하므로.
"""
from __future__ import annotations

from typing import Any

from ..openapi import _operations, resolve_pointer

#: 모델이 필드의 의미를 판단하는 데 실제로 쓰이는 키만 통과시킨다.
DIGEST_KEEP = frozenset({
    "type", "enum", "required", "format", "description", "pattern",
    "default", "minimum", "maximum", "minLength", "maxLength",
    "nullable", "title",
})

#: 값이 스키마인 map (properties는 "스키마 자체"가 아니라 "값이 스키마인 map").
DIGEST_MAPS = frozenset({"properties", "patternProperties"})

#: 값이 단일 스키마인 키.
DIGEST_ONE = frozenset({"items", "additionalProperties"})

#: 값이 스키마 list인 키.
DIGEST_LISTS = frozenset({"allOf", "anyOf", "oneOf"})

#: 하위 스키마를 담는 모든 키 - 재귀 대상.
DIGEST_STRUCT = DIGEST_MAPS | DIGEST_ONE | DIGEST_LISTS

MAX_DEPTH = 12

_PARAM_KEEP = ("name", "in", "required", "description")


def digest_schema(node: Any, spec: dict, *, seen: frozenset = frozenset(),
                  depth: int = 0, inline_refs: bool = True) -> Any:
    """스키마에서 의미 판단에 필요한 키만 남긴 사본을 만든다.

    inline_refs=True (pass 2a): $ref를 인라인 전개하되 순환은 끊는다.
    inline_refs=False (pass 2b): $ref를 {"$ref": "<짧은 이름>"}로 남긴다.
    공유 스키마 본문은 pass 2a가 스키마당 1회 처리하므로, operation마다
    펼치면 재사용 배수(중앙 2.33)만큼 입력이 중복된다.
    """
    if depth > MAX_DEPTH:
        return {}
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        short = ref.rsplit("/", 1)[-1]
        if not inline_refs or ref in seen:
            return {"$ref": short}
        target = resolve_pointer(spec, ref)
        if not isinstance(target, dict):
            return {"$ref": short}
        return digest_schema(target, spec, seen=seen | {ref}, depth=depth + 1,
                             inline_refs=inline_refs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in DIGEST_MAPS:
            if isinstance(value, dict):
                out[key] = {k: digest_schema(v, spec, seen=seen,
                                             depth=depth + 1, inline_refs=inline_refs)
                            for k, v in value.items()}
        elif key in DIGEST_ONE:
            # additionalProperties는 스키마일 수도, bool일 수도 있다
            # (false = 닫힌 객체). bool을 떨어뜨리면 모델은 추가 필드가
            # 금지된 것을 알 수 없다.
            out[key] = (digest_schema(value, spec, seen=seen,
                                      depth=depth + 1,
                                      inline_refs=inline_refs)
                        if isinstance(value, dict) else value)
        elif key in DIGEST_LISTS:
            if isinstance(value, list):
                out[key] = [digest_schema(v, spec, seen=seen, depth=depth + 1,
                                          inline_refs=inline_refs)
                            for v in value]
        elif key in DIGEST_KEEP:
            out[key] = value
    return out


def _first_json_schema(container: Any, spec: dict, *,
                       inline_refs: bool = True) -> Any:
    content = (container or {}).get("content")
    if not isinstance(content, dict):
        return {}
    for media, body in content.items():
        if isinstance(body, dict) and "json" in str(media):
            return digest_schema(body.get("schema") or {}, spec,
                                 inline_refs=inline_refs)
    return {}


def digest_operation(path: str, method: str, op: dict, spec: dict) -> dict:
    """operation 하나를 pass 2b 입력 형태로 압축한다."""
    params = []
    for prm in (op.get("parameters") or []):
        if not isinstance(prm, dict) or "$ref" in prm:
            continue
        item = {k: prm[k] for k in _PARAM_KEEP if k in prm}
        item["schema"] = digest_schema(prm.get("schema") or {}, spec,
                                       inline_refs=False)
        params.append(item)
    out = {
        "operationId": op.get("operationId"),
        "path": path,
        "method": method,
        "summary": op.get("summary"),
        "description": op.get("description"),
        "parameters": params,
        "input": _first_json_schema(op.get("requestBody"), spec, inline_refs=False),
    }
    ok = (op.get("responses") or {}).get("200")
    if isinstance(ok, dict):
        out["output"] = _first_json_schema(ok, spec, inline_refs=False)
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def spec_outline(spec: dict) -> dict:
    """pass 1 입력: 이름만 담은 뼈대 (스키마 본문 제외)."""
    from ..checks import _component_schemas

    ops = [op.get("operationId") for _, _, op in _operations(spec)
           if op.get("operationId")]
    types = {}
    for name, schema in (_component_schemas(spec) or {}).items():
        props = (schema or {}).get("properties")
        if isinstance(props, dict):
            types[name] = list(props)
    return {
        "service": ((spec.get("info") or {}).get("title") or ""),
        "operations": ops,
        "types": types,
    }
