"""Assemble the parsed WSDL model into an OpenAPI spec (3.0 or 3.1).

The spec is a valid, ordinary OpenAPI document that FastMCP's
`from_openapi()` can consume directly. SOAP binding metadata is embedded in
vendor extensions:

- root  `x-soap`  : wsdl source, generator info, skipped operations
- op    `x-soap`  : soapAction, soapVersion, style, endpoint, wrapper
                    element QNames, soap:header parts, declared faults
- schemas carry OpenAPI `xml` annotations (name / namespace / attribute /
  x-text) which a SOAP call layer uses to serialize JSON <-> literal XML.

Property order inside `properties` mirrors the XSD sequence order and MUST
be preserved (do not alphabetize the document).
"""
from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import __version__ as _version
from .errors import ConversionError

if TYPE_CHECKING:  # the SOAP stack (zeep/lxml) loads only when used
    from .parser import ParsedWsdl

_TOOL_ID_RE = re.compile(r"[^A-Za-z0-9_]+")
_MAX_ID_LEN = 64
_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def sanitize_name(name: str) -> str:
    """Clamp a raw WSDL/XSD name to a safe schema-name alphabet."""
    out = _NAME_RE.sub("_", name or "unnamed")
    return out[:64] or "unnamed"


def _tool_id(raw: str) -> str:
    """Normalize to FastMCP's tool-name alphabet, bounded to 64 chars."""
    tid = _TOOL_ID_RE.sub("_", sanitize_name(raw)).strip("_")
    return tid[:_MAX_ID_LEN] if tid else "op"


def _normalize_openapi_version(value: Any) -> str:
    """Validate a requested output version, returning '3.0' or '3.1'.

    Accepts the minor version with or without a patch suffix ('3.1',
    '3.1.0') and numeric 3.0/3.1; anything else raises ConversionError
    instead of silently emitting a different version than requested."""
    v = str(value).strip()
    if v == "3.0" or v.startswith("3.0."):
        return "3.0"
    if v == "3.1" or v.startswith("3.1."):
        return "3.1"
    raise ConversionError(
        f"unsupported openapi_version {value!r}: pass '3.0' or '3.1'"
    )


_HTTP_METHODS = frozenset(
    ("get", "put", "post", "delete", "options", "head", "patch", "trace")
)
# a safe MCP tool name before FastMCP normalization ('.'/'-' become '_')
_SAFE_TOOL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# FastMCP's tool-name normalization (per character, no run collapsing)
_FASTMCP_NORM_RE = re.compile(r"[^A-Za-z0-9_]")
# schema keywords whose *values* are data, not sub-schemas — a $ref (or
# nullable/enum/...) inside them is a data value, never something to walk
# or rewrite as schema. Shared by to_openapi_31 below, swagger.py's
# upgrader, and checks.py's $ref scanner (which adds "const", a 3.1-only
# data keyword the Swagger-2.0-only upgrader has no reason to know about).
_DATA_KEYWORDS = ("example", "examples", "default", "enum")


def _operations(spec: dict[str, Any]):
    """Yield (path, method, operation) for every HTTP operation."""
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        return
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            # only HTTP methods are operations; skip parameters/$ref/x- keys
            if str(method).lower() in _HTTP_METHODS and isinstance(op, dict):
                yield path, method, op


# --------------------------------------------------------------------------
# '#/...' ref / JSON-Pointer resolution
#
# Shared home for what used to be four diverging implementations (#142):
# bridge.py's schema-$ref dereference, minify.py's generic pointer walk,
# swagger.py's source-document pointer walk, and checks.py's schemas-only
# existence check. Each caller's job differs enough (bridge also unwraps
# single-member allOf and carries xml annotations; swagger also percent-
# decodes and indexes into lists, because it walks a hand-authored source
# document; checks only needs the first path segment, not a full walk) that
# full unification would change behavior, so callers delegate the part
# that is genuinely identical and keep their own extra logic layered on
# top — see each module for the specifics.
# --------------------------------------------------------------------------

_SCHEMA_REF_PREFIX = "#/components/schemas/"


def schema_ref_name(ref: Any) -> str | None:
    """The component name addressed by a '#/components/schemas/NAME...'
    ref — only the segment right after the prefix, so a deeper pointer
    (e.g. '.../NAME/properties/x') still yields NAME. None if `ref` isn't
    a string, or doesn't address components.schemas at all."""
    if not (isinstance(ref, str) and ref.startswith(_SCHEMA_REF_PREFIX)):
        return None
    return ref[len(_SCHEMA_REF_PREFIX):].split("/", 1)[0]


def _unescape_pointer_token(token: str) -> str:
    """Undo RFC 6901 JSON Pointer escaping ('~1' -> '/', '~0' -> '~')."""
    return token.replace("~1", "/").replace("~0", "~")


def resolve_pointer(root: Any, ref: Any) -> Any:
    """Resolve an internal '#/...' JSON Pointer against `root`.

    Dict traversal only (no list indexing) with RFC 6901 ~0/~1 token
    unescaping and no percent-decoding — root is always a generated-or-
    loaded OpenAPI document here, never a URI fragment. None if
    unresolvable (or if the pointer legitimately addresses a null)."""
    if not (isinstance(ref, str) and ref.startswith("#/")):
        return None
    node: Any = root
    for part in ref[2:].split("/"):
        part = _unescape_pointer_token(part)
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def check_fastmcp_ready(spec: dict[str, Any]) -> list[str]:
    """Static FastMCP-readiness check; an empty list means ready.

    Verifies the contract behind ``spec2openapi validate`` without
    importing fastmcp: the document has operations, every operation has
    an operationId that is a safe MCP tool name and stays unique even
    after FastMCP's ``[A-Za-z0-9_]`` normalization, and SOAP operations
    carry their wrapper element. Returns one message per problem.

    This check set is frozen; `spec2openapi.verify` runs the superset."""
    from .checks import fastmcp_ready_problems

    return fastmcp_ready_problems(spec)


def _unique_id(base: str, used: set[str]) -> str:
    """Make base unique within `used`, keeping the result <= 64 chars."""
    if base not in used:
        used.add(base)
        return base
    n = 2
    while True:
        suffix = f"_{n}"
        candidate = base[: _MAX_ID_LEN - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1

SOAP_FAULT_SCHEMA = {
    "type": "object",
    "description": "SOAP Fault mapped to a JSON error payload.",
    "properties": {
        "faultcode": {"type": "string"},
        "faultstring": {"type": "string"},
        "detail": {"type": "string"},
    },
}


def _origin(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return url or "http://localhost"


def _element_qname(element: Any) -> dict[str, Any]:
    qname = getattr(element, "qname", None)
    if qname is None:
        return {"element": getattr(element, "name", ""), "namespace": None}
    return {"element": qname.localname, "namespace": qname.namespace}


def build_spec(
    parsed: ParsedWsdl,
    *,
    title: str | None = None,
    version: str = "1.0.0",
    base_path: str = "/operations",
    openapi_version: str = "3.0",
) -> dict[str, Any]:
    """Assemble a parse_wsdl model into an OpenAPI dict with x-soap
    extensions (the second half of convert_wsdl)."""
    from .schema import SchemaConverter

    openapi_version = _normalize_openapi_version(openapi_version)
    # Paths Object keys must start with '/'
    base_path = base_path or ""
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    if not parsed.operations:
        detail = ""
        if parsed.skipped:
            reasons = "; ".join(f"{op}: {r}" for op, r in parsed.skipped)
            detail = f" {len(parsed.skipped)} operation(s) were skipped ({reasons})."
        raise ConversionError(
            f"no convertible SOAP operations found in '{parsed.source}'."
            f"{detail} Nothing to generate — check that the WSDL exposes "
            "document/literal or rpc/literal SOAP bindings."
        )
    conv = SchemaConverter(parsed.xsd_meta, zschema=parsed.schema)
    paths: dict[str, Any] = {}
    used_ids: set[str] = set()

    # reserve the built-in fault schema name up front so a WSDL type also
    # named "SoapFault" is deduped to another name instead of clobbering it.
    # deep-copied: conv.components[...] used to alias the module-level
    # SOAP_FAULT_SCHEMA dict by reference, so every conversion (and the
    # template itself) shared and could corrupt one mutable object (#142)
    fault_ref_name = "SoapFault"
    conv.components[fault_ref_name] = copy.deepcopy(SOAP_FAULT_SCHEMA)
    fault_ref = f"#/components/schemas/{fault_ref_name}"

    for op in parsed.operations:
        # FastMCP normalizes tool names to [A-Za-z0-9_]; emit operationIds
        # in that alphabet, bounded to 64 chars, and re-checked for
        # uniqueness *after* normalization/truncation so two operations
        # never collide onto the same path (which would drop one).
        op_id = _unique_id(_tool_id(op.op_id), used_ids)
        in_q = _element_qname(op.input_element)
        in_schema = conv.element_type_to_object_schema(
            op.input_element.type,
            hint=f"{op_id}Input",
            qkey=(in_q["namespace"] or "", in_q["element"]),
        )
        has_params = bool(in_schema.get("properties"))

        if op.output_element is not None:
            out_q = _element_qname(op.output_element)
            out_schema = conv.element_type_to_object_schema(
                op.output_element.type,
                hint=f"{op_id}Output",
                qkey=(out_q["namespace"] or "", out_q["element"]),
            )
        else:
            out_schema = {"type": "object"}

        x_soap: dict[str, Any] = {
            "operation": op.name,
            "service": op.service,
            "port": op.port,
            "soapAction": op.soap_action,
            "soapVersion": op.soap_version,
            "style": op.style,
            "endpoint": op.endpoint,
            "input": _element_qname(op.input_element),
        }
        if op.output_element is not None:
            x_soap["output"] = _element_qname(op.output_element)

        doc_lines = [
            op.documentation
            or f"SOAP operation {op.name} of service {op.service}."
        ]

        if op.headers:
            hmeta = []
            for h in op.headers:
                comp = conv.register_element_component(
                    h.element, hint=f"{op_id}Header_{h.part}"
                )
                entry = _element_qname(h.element)
                entry["part"] = h.part
                entry["schema"] = f"#/components/schemas/{comp}"
                hmeta.append(entry)
            x_soap["headers"] = hmeta
            names = ", ".join(h["element"] for h in hmeta)
            doc_lines.append(
                f"Requires SOAP header(s): {names} "
                "(supplied by the runtime, not by tool arguments)."
            )

        if op.faults:
            fmeta = []
            fault_names = []
            for f in op.faults:
                entry: dict[str, Any] = {"name": f.name}
                if f.element is not None:
                    entry.update(_element_qname(f.element))
                    comp = conv.register_element_component(
                        f.element, hint=f"{op_id}Fault_{f.name}"
                    )
                    entry["schema"] = f"#/components/schemas/{comp}"
                fmeta.append(entry)
                fault_names.append(f.name)
            x_soap["faults"] = fmeta
            doc_lines.append(f"Declared faults: {', '.join(fault_names)}.")

        description = "\n".join(doc_lines)
        post: dict[str, Any] = {
            "operationId": op_id,
            "summary": doc_lines[0].splitlines()[0][:120],
            "description": description,
            "tags": [op.service],
            "x-soap": x_soap,
            "requestBody": {
                "required": has_params,
                "content": {"application/json": {"schema": in_schema}},
            },
            "responses": {
                "200": {
                    "description": f"Result of SOAP operation {op.name}",
                    "content": {"application/json": {"schema": out_schema}},
                },
                "500": {
                    "description": "SOAP Fault"
                    + (
                        f" (declared: {', '.join(f.name for f in op.faults)})"
                        if op.faults
                        else ""
                    ),
                    "content": {
                        "application/json": {
                            "schema": {"$ref": fault_ref}
                        }
                    },
                },
            },
        }
        paths[f"{base_path}/{op_id}"] = {"post": post}

    endpoint = parsed.operations[0].endpoint if parsed.operations else ""
    components = dict(conv.components)  # already includes the fault schema

    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": title or parsed.name,
            "version": version,
            "description": parsed.documentation
            or f"Generated from WSDL by spec2openapi {_version}. "
            f"Each path is a SOAP operation exposed as a JSON call.",
        },
        "servers": [{"url": _origin(endpoint)}],
        "paths": paths,
        "components": {"schemas": components},
        "x-soap": {
            "wsdl": parsed.source,
            "generator": f"spec2openapi/{_version}",
            "skippedOperations": [
                {"operation": o, "reason": r} for o, r in parsed.skipped
            ],
        },
    }
    if openapi_version.startswith("3.1"):
        spec = to_openapi_31(spec)
    return spec


# walk() recurses once per document-nesting level; a pathologically deep
# document must raise ConversionError, never a bare RecursionError (see
# swagger.py's _MAX_SCHEMA_DEPTH for the same guard and rationale).
_MAX_WALK_DEPTH = 200


def to_openapi_31(spec: dict[str, Any]) -> dict[str, Any]:
    """Convert the generated 3.0 document to OpenAPI 3.1 JSON Schema style."""

    # keywords whose values are data, not sub-schemas — don't descend
    data_kw = _DATA_KEYWORDS
    # keys whose value is a name -> schema map: the map's own keys are
    # opaque property names, not schema keywords — a property literally
    # named "nullable"/"enum"/"exclusiveMinimum" must not trigger the
    # keyword handling below (mirrors swagger.py's _fix_schema, which
    # already applies this rule on the way to 3.0).
    map_kw = ("properties", "patternProperties")
    # 이름->객체 map: 키는 사용자가 정한 이름이므로 data 키워드로
    # 볼 수 없다. responses 의 "default" 가 대표적이다 - 스키마의
    # default 값과 이름이 같을 뿐 전혀 다른 것이라, 통째로 복사하면
    # 그 안의 nullable 이 변환되지 않은 채 3.1 문서에 남는다.
    # "examples"/"content" 는 넣지 않는다: Example Object 의 value 는
    # 사용자 데이터라 walk 하면 그 안의 nullable 을 스키마로 착각해
    # 고쳐버린다. 미디어 타입 이름도 data 키워드와 겹칠 수 없다.
    named_maps = ("responses", "headers", "parameters", "schemas",
                  "requestBodies", "securitySchemes", "links",
                  "callbacks", "pathItems")

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > _MAX_WALK_DEPTH:
            raise ConversionError(
                f"schema nesting exceeds {_MAX_WALK_DEPTH} levels; "
                "refusing to convert (a legitimate document is never "
                "this deep)"
            )
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in map_kw and isinstance(v, dict):
                out[k] = {pn: walk(pv, depth + 1) for pn, pv in v.items()}
            elif k in named_maps and isinstance(v, dict):
                out[k] = {pn: walk(pv, depth + 1) for pn, pv in v.items()}
            elif k in data_kw:
                out[k] = v
            else:
                out[k] = walk(v, depth + 1)
        node = out
        if node.pop("nullable", False):
            t = node.get("type")
            if isinstance(t, str):
                node["type"] = [t, "null"]
            elif isinstance(t, list):
                if "null" not in t:
                    node["type"] = [*t, "null"]
            elif node:
                # can't fold into 'type': the schema is $ref/allOf-
                # wrapped, enum-only, or otherwise typeless. Compose
                # instead of dropping — 'anyOf: [X, {type: null}]' always
                # stays exact (matches X, or is null) for any X, so this
                # is never actually lossy (#141 A5).
                rest = dict(node)
                node.clear()
                node["anyOf"] = [rest, {"type": "null"}]
            # else: node is now empty ('nullable: true' was the only
            # key) — {} already matches every value including null.
        # 3.0 uses boolean exclusiveMinimum/Maximum alongside minimum/maximum;
        # 2020-12 requires a number. Convert true+bound, and drop the boolean
        # otherwise (false = inclusive default; true without a bound is
        # malformed and cannot be represented).
        for kw, bound in (("exclusiveMinimum", "minimum"),
                          ("exclusiveMaximum", "maximum")):
            val = node.get(kw)
            if isinstance(val, bool):
                if val and bound in node:
                    node[kw] = node.pop(bound)
                else:
                    node.pop(kw, None)
        return node

    out = walk(dict(spec))
    out["openapi"] = "3.1.0"
    return out


def dump_spec(spec: dict[str, Any], fmt: str = "yaml") -> str:
    """Serialize a spec to YAML (default) or JSON (`fmt="json"`) text.

    Key order is preserved — property order mirrors the XSD sequence and
    is significant for SOAP serialization."""
    if fmt == "json":
        import json

        return json.dumps(spec, indent=2, ensure_ascii=False)
    import yaml

    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)
