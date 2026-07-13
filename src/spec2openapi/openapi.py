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

from typing import Any
from urllib.parse import urlparse

from . import __version__ as _version
from .errors import ConversionError
from .parser import ParsedWsdl
import re

from .schema import SchemaConverter, sanitize_name

_TOOL_ID_RE = re.compile(r"[^A-Za-z0-9_]+")
_MAX_ID_LEN = 64


def _tool_id(raw: str) -> str:
    """Normalize to FastMCP's tool-name alphabet, bounded to 64 chars."""
    tid = _TOOL_ID_RE.sub("_", sanitize_name(raw)).strip("_")
    return tid[:_MAX_ID_LEN] if tid else "op"


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
    conv = SchemaConverter(parsed.xsd_meta)
    paths: dict[str, Any] = {}
    used_ids: set[str] = set()

    # reserve the built-in fault schema name up front so a WSDL type also
    # named "SoapFault" is deduped to another name instead of clobbering it
    fault_ref_name = "SoapFault"
    conv.components[fault_ref_name] = SOAP_FAULT_SCHEMA
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


def to_openapi_31(spec: dict[str, Any]) -> dict[str, Any]:
    """Convert the generated 3.0 document to OpenAPI 3.1 JSON Schema style."""

    # keywords whose values are data, not sub-schemas — don't descend
    data_kw = ("example", "examples", "default", "enum")

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        node = {k: (v if k in data_kw else walk(v)) for k, v in node.items()}
        if node.pop("nullable", False):
            t = node.get("type")
            if isinstance(t, str):
                node["type"] = [t, "null"]
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
    if fmt == "json":
        import json

        return json.dumps(spec, indent=2, ensure_ascii=False)
    import yaml

    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)
