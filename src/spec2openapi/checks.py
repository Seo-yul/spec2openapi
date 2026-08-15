"""Structured verification of converted specs.

`verify(spec)` runs a registry of checks over an OpenAPI document and
returns a deterministic, JSON-serializable report. Adapted from the
design of modelcontextprotocol/conformance: structured per-check results,
normative citations (refs), and explicit skip-vs-fail separation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
