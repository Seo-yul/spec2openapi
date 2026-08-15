"""Structured verification of converted specs.

`verify(spec)` runs a registry of checks over an OpenAPI document and
returns a deterministic, JSON-serializable report. Adapted from the
design of modelcontextprotocol/conformance: structured per-check results,
normative citations (refs), and explicit skip-vs-fail separation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .openapi import _FASTMCP_NORM_RE, _SAFE_TOOL_RE, _operations


@dataclass(frozen=True)
class CheckRef:
    """A normative citation backing a check (spec section, contract doc)."""
    id: str
    url: str = ""


@dataclass(frozen=True)
class CheckResult:
    id: str
    status: str                # "pass" | "warn" | "fail" | "skip"
    message: str = ""
    location: str = ""
    refs: tuple[CheckRef, ...] = ()
    data: dict | None = None


@dataclass(frozen=True)
class VerifyReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.status != "fail" for r in self.results)

    @property
    def complete(self) -> bool:
        return all(r.status != "skip" for r in self.results)

    def to_dict(self) -> dict:
        summary = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
        for r in self.results:
            summary[r.status] += 1
        return {
            "ok": self.ok,
            "complete": self.complete,
            "summary": summary,
            "results": [
                {
                    "id": r.id, "status": r.status, "message": r.message,
                    "location": r.location,
                    "refs": [{"id": ref.id, "url": ref.url} for ref in r.refs],
                    "data": r.data,
                }
                for r in self.results
            ],
        }


_REF_SEP986 = CheckRef(
    "SEP-986",
    "https://modelcontextprotocol.io/specification/2025-11-25/server/tools#tool-names",
)
_REF_TOOLS_LIST = CheckRef(
    "MCP-Tools-List",
    "https://modelcontextprotocol.io/specification/2025-11-25/server/tools#listing-tools",
)
_REF_OAS = CheckRef("OAS-3.x", "https://spec.openapis.org/oas/latest.html")
_REF_FASTMCP = CheckRef("FastMCP-from-openapi", "https://gofastmcp.com")
_REF_XSOAP = CheckRef(
    "x-soap-contract",
    "https://github.com/Seo-yul/spec2openapi#how-soap-calls-work-the-x-soap-contract",
)

# Frozen registry: statement/level changes are deliberate, reviewable
# contract changes (see the design doc). level derivation: violations that
# break the runtime (MUST-equivalent) are "fail"; quality/interop issues or
# violations with a runtime fallback (SHOULD-equivalent) are "warn".
REGISTRY: dict[str, dict] = {
    "document.mapping": {
        "statement": "The document must be a JSON/YAML mapping.",
        "level": "fail", "refs": (_REF_OAS,)},
    "document.has-paths": {
        "statement": "The document must declare a non-empty paths object.",
        "level": "fail", "refs": (_REF_OAS, _REF_FASTMCP)},
    "document.has-operations": {
        "statement": "paths must contain at least one HTTP operation.",
        "level": "fail", "refs": (_REF_OAS, _REF_FASTMCP)},
    "document.openapi3": {
        "statement": "The openapi field must declare a 3.x version; "
                     "Swagger 2.0 input must be converted first.",
        "level": "fail", "refs": (_REF_OAS,)},
    "tool-name.present": {
        "statement": "Every operation must carry an operationId; it becomes "
                     "the MCP tool name.",
        "level": "fail", "refs": (_REF_TOOLS_LIST, _REF_FASTMCP)},
    "tool-name.safe": {
        "statement": "operationId must match [A-Za-z0-9_.-]{1,64} (a subset "
                     "of the SEP-986 tool-name grammar).",
        "level": "fail", "refs": (_REF_SEP986, _REF_FASTMCP)},
    "tool-name.unique": {
        "statement": "operationIds must be unique across the document.",
        "level": "fail", "refs": (_REF_TOOLS_LIST, _REF_FASTMCP)},
    "tool-name.normalization-collision": {
        "statement": "Distinct operationIds must not collide after FastMCP "
                     "normalization ([^A-Za-z0-9_] -> _).",
        "level": "fail", "refs": (_REF_FASTMCP,)},
    "tool-name.normalized": {
        "statement": "operationIds that FastMCP renames on exposure are "
                     "surprising; prefer ids already in [A-Za-z0-9_].",
        "level": "warn", "refs": (_REF_FASTMCP,)},
    "tool-description.present": {
        "statement": "Every operation should carry a description or summary; "
                     "the official conformance tools-list check flags tools "
                     "without a description.",
        "level": "warn", "refs": (_REF_TOOLS_LIST,)},
    "x-soap.input-element": {
        "statement": "A SOAP operation must name its input wrapper element "
                     "(x-soap.input.element).",
        "level": "fail", "refs": (_REF_XSOAP,)},
    "x-soap.version": {
        "statement": "x-soap.soapVersion must be '1.1' or '1.2'; any other "
                     "value silently degrades to 1.1 in the bridge.",
        "level": "fail", "refs": (_REF_XSOAP,)},
    "x-soap.output-element": {
        "statement": "x-soap.output should name its element; without it "
                     "response Body matching cannot be verified.",
        "level": "warn", "refs": (_REF_XSOAP,)},
    "x-soap.endpoint": {
        "statement": "x-soap.endpoint should be present (SPEC2OPENAPI_ENDPOINT "
                     "can override it at runtime).",
        "level": "warn", "refs": (_REF_XSOAP,)},
    "x-soap.refs": {
        "statement": "Schema references in x-soap headers/faults and in a SOAP "
                     "operation's request/response bodies must resolve to "
                     "components.schemas.",
        "level": "fail", "refs": (_REF_XSOAP, _REF_OAS)},
    "x-soap.substitution": {
        "statement": "x-soap-substitution markers must list members with "
                     "element names, and oneOf branches must be "
                     "self-describing single-property objects matching them.",
        "level": "fail", "refs": (_REF_XSOAP,)},
    "x-soap.choice": {
        "statement": "x-soap-choice groups must reference existing properties.",
        "level": "fail", "refs": (_REF_XSOAP,)},
    "x-soap.mixed-rest": {
        "statement": "SOAP and plain REST operations should not share one "
                     "document; the reference runtime routes all traffic "
                     "through the SOAP bridge when any x-soap path exists.",
        "level": "warn", "refs": (_REF_XSOAP,)},
    "openapi.schema-valid": {
        "statement": "The document must validate against the OpenAPI schema "
                     "(openapi-spec-validator).",
        "level": "fail", "refs": (_REF_OAS,)},
    "fastmcp.roundtrip": {
        "statement": "FastMCP.from_openapi must build a server and list its "
                     "tools from the document.",
        "level": "fail", "refs": (_REF_FASTMCP,)},
    "fastmcp.tool-materialized": {
        "statement": "Every operation must materialize as an MCP tool in the "
                     "FastMCP round-trip.",
        "level": "fail", "refs": (_REF_FASTMCP, _REF_TOOLS_LIST)},
}


def _result(cid: str, status: str, message: str = "", location: str = "",
            data: dict | None = None) -> CheckResult:
    return CheckResult(id=cid, status=status, message=message,
                       location=location, refs=REGISTRY[cid]["refs"],
                       data=data)


def _op_location(path: object, method: object) -> str:
    return f"paths.{path}.{method}"


def _check_has_paths(spec):
    if not spec.get("paths"):
        return [_result("document.has-paths", "fail", "spec has no paths")]
    return []


def _check_has_operations(spec):
    if spec.get("paths") and not any(True for _ in _operations(spec)):
        return [_result("document.has-operations", "fail",
                        "spec has no operations")]
    return []


def _check_tool_name_present(spec):
    out = []
    for path, method, op in _operations(spec):
        if not op.get("operationId"):
            out.append(_result(
                "tool-name.present", "fail",
                f"{str(method).upper()} {path}: missing operationId",
                location=_op_location(path, method)))
    return out


def _check_tool_name_safe(spec):
    out = []
    for path, method, op in _operations(spec):
        oid = op.get("operationId")
        if not oid:
            continue
        if not isinstance(oid, str) or not _SAFE_TOOL_RE.fullmatch(oid):
            out.append(_result(
                "tool-name.safe", "fail",
                f"{oid}: not a safe MCP tool name",
                location=_op_location(path, method)))
    return out


def _check_tool_name_unique(spec):
    op_ids = [op.get("operationId") for _, _, op in _operations(spec)
              if op.get("operationId")]
    dupes = {o for o in op_ids if op_ids.count(o) > 1}
    if dupes:
        return [_result("tool-name.unique", "fail",
                        f"duplicate operationIds: {sorted(dupes)}")]
    return []


def _check_normalization_collision(spec):
    by_tool: dict[str, set[str]] = {}
    for _, _, op in _operations(spec):
        oid = op.get("operationId")
        if isinstance(oid, str) and oid:
            by_tool.setdefault(_FASTMCP_NORM_RE.sub("_", oid), set()).add(oid)
    out = []
    for tool, oids in sorted(by_tool.items()):
        if len(oids) > 1:
            out.append(_result(
                "tool-name.normalization-collision", "fail",
                "operationIds collide after FastMCP normalization "
                f"('{tool}'): {sorted(oids)}"))
    return out


def _check_xsoap_input_element(spec):
    out = []
    for path, method, op in _operations(spec):
        oid = op.get("operationId")
        if not oid:
            continue
        xsoap = op.get("x-soap")
        if isinstance(xsoap, dict):
            inp = xsoap.get("input")
            if not (isinstance(inp, dict) and inp.get("element")):
                out.append(_result(
                    "x-soap.input-element", "fail",
                    f"{oid}: x-soap.input.element missing",
                    location=_op_location(path, method)))
    return out


# (id, fn) — 실행 순서; 보고서는 어차피 (id, location)으로 재정렬된다.
# frozen 8종 중 document.mapping은 verify()/fastmcp_ready_problems()의
# 입구 가드로 처리되므로 목록에 없다.
_STATIC_CHECKS: list = [
    ("document.has-paths", _check_has_paths),
    ("document.has-operations", _check_has_operations),
    ("tool-name.present", _check_tool_name_present),
    ("tool-name.safe", _check_tool_name_safe),
    ("tool-name.unique", _check_tool_name_unique),
    ("tool-name.normalization-collision", _check_normalization_collision),
    ("x-soap.input-element", _check_xsoap_input_element),
]

_READY_IDS = frozenset((
    "document.has-paths", "document.has-operations", "tool-name.present",
    "tool-name.safe", "tool-name.unique", "tool-name.normalization-collision",
    "x-soap.input-element",
))


def fastmcp_ready_problems(spec) -> list[str]:
    """The frozen check_fastmcp_ready contract: one message per problem."""
    if not isinstance(spec, dict):
        return ["not an OpenAPI document (expected a mapping)"]
    out: list[str] = []
    for cid, fn in _STATIC_CHECKS:
        if cid in _READY_IDS:
            out.extend(r.message for r in fn(spec) if r.status == "fail")
    return out
