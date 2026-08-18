"""Structured verification of converted specs.

`verify(spec)` runs a registry of checks over an OpenAPI document and
returns a deterministic, JSON-serializable report. Adapted from the
design of modelcontextprotocol/conformance: structured per-check results,
normative citations (refs), and explicit skip-vs-fail separation.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Any

from .errors import MCP_HINT
from .openapi import (
    _DATA_KEYWORDS,
    _FASTMCP_NORM_RE,
    _SAFE_TOOL_RE,
    _SCHEMA_REF_PREFIX,
    _operations,
    schema_ref_name,
)
from .swagger import is_swagger2


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
                    # shallow copy: to_dict() must not leak a reference a
                    # caller could mutate to corrupt this (frozen)
                    # CheckResult's internal data dict.
                    "data": dict(r.data) if r.data is not None else None,
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
        "statement": "An invalid soapVersion is always a defect (the "
                     "bridge silently degrades to 1.1, misbehaving "
                     "against 1.2 services) — fail; an absent soapVersion "
                     "falls back to the documented 1.1 default — warn.",
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
        "statement": "Every operation must materialize as an MCP tool: the "
                     "FastMCP round-trip's tool count must equal the "
                     "document's operation count (FastMCP's actual "
                     "tool-naming/normalization differs across versions and "
                     "cannot be predicted, so the check is a count "
                     "invariant, not a name match).",
        "level": "fail", "refs": (_REF_FASTMCP, _REF_TOOLS_LIST)},
}


def _result(cid: str, status: str, message: str = "", location: str = "",
            data: dict | None = None) -> CheckResult:
    return CheckResult(id=cid, status=status, message=message,
                       location=location, refs=REGISTRY[cid]["refs"],
                       data=data)


def _finding(cid: str, message: str, location: str = "") -> CheckResult:
    """A warn/fail finding whose status is the check's registered level."""
    return _result(cid, REGISTRY[cid]["level"], message, location)


def _op_location(path: object, method: object) -> str:
    return f"paths.{path}.{method}"


def _op_display(op: dict, path: object, method: object) -> str:
    """The operationId, or a "METHOD path" fallback when it is absent."""
    return op.get("operationId") or f"{str(method).upper()} {path}"


def _check_has_paths(spec):
    if not spec.get("paths"):
        return [_finding("document.has-paths", "spec has no paths")]
    return []


def _check_has_operations(spec):
    if not spec.get("paths"):
        # document.has-paths already fails this document; without paths
        # there is nothing to check operations against, so say so
        # instead of silently reporting a dishonest pass.
        return [_result("document.has-operations", "skip",
                        "not checked: spec has no paths")]
    if not any(True for _ in _operations(spec)):
        return [_finding("document.has-operations", "spec has no operations")]
    return []


def _check_tool_name_present(spec):
    out = []
    for path, method, op in _operations(spec):
        if not op.get("operationId"):
            out.append(_finding(
                "tool-name.present",
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
            out.append(_finding(
                "tool-name.safe",
                f"{oid}: not a safe MCP tool name",
                location=_op_location(path, method)))
    return out


def _check_tool_name_unique(spec):
    op_ids = [op.get("operationId") for _, _, op in _operations(spec)
              if op.get("operationId")]
    # operationId is untrusted input and may be unhashable (list/dict);
    # split into a hashable group (Counter, O(n)) and an unhashable group
    # (grouped by repr) instead of a set comprehension over op_ids, which
    # both required hashability and re-scanned the list per id (O(n^2)).
    hashable, unhashable = [], []
    for o in op_ids:
        try:
            hash(o)
        except TypeError:
            unhashable.append(o)
        else:
            hashable.append(o)
    dupes = [o for o, n in collections.Counter(hashable).items() if n > 1]
    by_repr: dict[str, list] = {}
    for o in unhashable:
        by_repr.setdefault(repr(o), []).append(o)
    dupes.extend(group[0] for group in by_repr.values() if len(group) > 1)
    if dupes:
        # previously-non-crashing input (homogeneous, comparable types)
        # must keep its natural sort order; only fall back to repr-based
        # ordering for genuinely incomparable/unhashable duplicates.
        try:
            rendered = sorted(dupes)
        except TypeError:
            rendered = sorted(dupes, key=repr)
        return [_finding("tool-name.unique",
                         f"duplicate operationIds: {rendered}")]
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
            out.append(_finding(
                "tool-name.normalization-collision",
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
                out.append(_finding(
                    "x-soap.input-element",
                    f"{oid}: x-soap.input.element missing",
                    location=_op_location(path, method)))
    return out


def _check_openapi3(spec):
    if is_swagger2(spec):
        return [_finding("document.openapi3",
                         "Swagger 2.0 document: run convert_swagger first")]
    v = spec.get("openapi")
    if not (isinstance(v, str) and v.startswith("3.")):
        return [_finding("document.openapi3",
                         f"openapi version {v!r} is not 3.x")]
    return []


def _check_tool_name_normalized(spec):
    out = []
    for path, method, op in _operations(spec):
        oid = op.get("operationId")
        if (isinstance(oid, str) and _SAFE_TOOL_RE.fullmatch(oid)):
            norm = _FASTMCP_NORM_RE.sub("_", oid)
            if norm != oid:
                out.append(_finding(
                    "tool-name.normalized",
                    f"operationId '{oid}' is exposed as tool '{norm}' "
                    "(FastMCP normalization)",
                    location=_op_location(path, method)))
    return out


def _check_tool_description(spec):
    out = []
    for path, method, op in _operations(spec):
        if not (op.get("description") or op.get("summary")):
            name = _op_display(op, path, method)
            out.append(_finding(
                "tool-description.present",
                f"{name}: operation has neither description nor summary; "
                "the MCP tool will expose no description",
                location=_op_location(path, method)))
    return out


def _soap_operations(spec):
    for path, method, op in _operations(spec):
        xsoap = op.get("x-soap")
        if isinstance(xsoap, dict):
            yield path, method, op, xsoap


def _check_xsoap_version(spec):
    # not routed through _finding(): this check has two levels for one
    # id (absent soapVersion -> warn, invalid soapVersion -> fail), so
    # the status must stay explicit at each call site rather than come
    # from REGISTRY's single registered level.
    out = []
    for path, method, op, xsoap in _soap_operations(spec):
        sv = xsoap.get("soapVersion")
        oid = _op_display(op, path, method)
        loc = _op_location(path, method)
        if sv is None:
            # the bridge falls back to 1.1 when soapVersion is absent —
            # a documented default, not a defect, so warn rather than fail
            out.append(_result(
                "x-soap.version", "warn",
                f"{oid}: x-soap.soapVersion missing; the bridge assumes "
                "1.1 — declare it explicitly", location=loc))
        elif sv not in ("1.1", "1.2"):
            out.append(_result(
                "x-soap.version", "fail",
                f"{oid}: x-soap.soapVersion {sv!r} is not '1.1' or '1.2'",
                location=loc))
    return out


def _check_xsoap_output_element(spec):
    out = []
    for path, method, op, xsoap in _soap_operations(spec):
        if "output" in xsoap:
            meta = xsoap["output"]
            if not (isinstance(meta, dict) and meta.get("element")):
                oid = _op_display(op, path, method)
                out.append(_finding(
                    "x-soap.output-element",
                    f"{oid}: x-soap.output.element missing; response Body "
                    "matching cannot be verified",
                    location=_op_location(path, method)))
    return out


def _check_xsoap_endpoint(spec):
    out = []
    for path, method, op, xsoap in _soap_operations(spec):
        if not xsoap.get("endpoint"):
            oid = _op_display(op, path, method)
            out.append(_finding(
                "x-soap.endpoint",
                f"{oid}: x-soap.endpoint missing (set SPEC2OPENAPI_ENDPOINT "
                "at runtime)",
                location=_op_location(path, method)))
    return out


def _check_xsoap_mixed_rest(spec):
    has_soap = any(True for _ in _soap_operations(spec))
    has_rest = any(not isinstance(op.get("x-soap"), dict)
                   for _, _, op in _operations(spec))
    if has_soap and has_rest:
        return [_finding(
            "x-soap.mixed-rest",
            "spec mixes x-soap and plain REST operations; the reference "
            "runtime routes all traffic through the SOAP bridge, so REST "
            "operations are not served correctly")]
    return []


# schema keywords whose *values* are data, not sub-schemas, so a
# dict shaped like {"$ref": ...} inside them is a data value, not an
# actual reference to resolve. Reuses openapi.py's _DATA_KEYWORDS (the
# set applied when the upgrader protects data values from schema
# rewriting) plus "const", a 3.1/2020-12 data keyword the Swagger-2.0-
# only upgrader has no reason to know about.
_DATA_SUBTREE_KEYS = frozenset(_DATA_KEYWORDS) | {"const"}


def _component_schemas(spec: dict) -> dict:
    comp = spec.get("components")
    schemas = comp.get("schemas") if isinstance(comp, dict) else None
    return schemas if isinstance(schemas, dict) else {}


def _iter_ref_strings(node):
    """Yield every "$ref" string in a JSON tree (no ref following).

    Subtrees under a data keyword (example/examples/default/enum/const)
    are skipped: their values are data, not schema, and may incidentally
    contain a dict shaped like {"$ref": ...}. Explicit-stack iteration
    (not recursive) so a deeply nested document cannot RecursionError."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            ref = cur.get("$ref")
            if isinstance(ref, str):
                yield ref
            children = [v for k, v in cur.items()
                       if k not in _DATA_SUBTREE_KEYS]
            stack.extend(reversed(children))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _check_xsoap_refs(spec):
    out = []
    schemas = _component_schemas(spec)

    def _resolvable(ref: str) -> bool:
        # a pointer into a component (.../A/properties/b) only needs its
        # first segment (the component name) to exist
        name = schema_ref_name(ref)
        return name is not None and name in schemas

    for path, method, op, xsoap in _soap_operations(spec):
        oid = _op_display(op, path, method)
        loc = _op_location(path, method)
        for kind in ("headers", "faults"):
            entries = xsoap.get(kind)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                ref = entry.get("schema") if isinstance(entry, dict) else None
                if isinstance(ref, str) and not _resolvable(ref):
                    out.append(_finding(
                        "x-soap.refs",
                        f"{oid}: x-soap.{kind} schema '{ref}' does not "
                        "resolve to components.schemas", location=loc))
        for section in (op.get("requestBody"), op.get("responses")):
            for ref in _iter_ref_strings(section):
                if ref.startswith(_SCHEMA_REF_PREFIX) and not _resolvable(ref):
                    out.append(_finding(
                        "x-soap.refs",
                        f"{oid}: schema ref '{ref}' does not resolve to "
                        "components.schemas", location=loc))
    return out


def _iter_schema_nodes(spec):
    """Yield (location, dict-node) over components.schemas and the
    request/response schema trees of x-soap operations. Walks the JSON
    tree without following $refs, so termination is structural."""
    def _walk(node, loc):
        # explicit-stack DFS (not recursive): a deeply nested schema
        # tree must not RecursionError. Yield order matches the
        # original recursive pre-order traversal exactly.
        stack = [(node, loc)]
        while stack:
            cur, cur_loc = stack.pop()
            if isinstance(cur, dict):
                yield cur_loc, cur
                children = [(v, f"{cur_loc}.{k}") for k, v in cur.items()]
                stack.extend(reversed(children))
            elif isinstance(cur, list):
                children = [(v, f"{cur_loc}[{i}]")
                           for i, v in enumerate(cur)]
                stack.extend(reversed(children))

    for name, schema in _component_schemas(spec).items():
        yield from _walk(schema, f"components.schemas.{name}")
    for path, method, op, _ in _soap_operations(spec):
        base = _op_location(path, method)
        yield from _walk(op.get("requestBody"), f"{base}.requestBody")
        yield from _walk(op.get("responses"), f"{base}.responses")


def _check_xsoap_substitution(spec):
    out = []
    for loc, node in _iter_schema_nodes(spec):
        sub = node.get("x-soap-substitution")
        if not isinstance(sub, dict):
            continue
        members = sub.get("members")
        if not isinstance(members, list):
            out.append(_finding("x-soap.substitution",
                                "x-soap-substitution.members missing",
                                location=loc))
            continue
        member_els = set()
        bad_member = False
        for m in members:
            el = m.get("element") if isinstance(m, dict) else None
            if not isinstance(el, str) or not el:
                out.append(_finding(
                    "x-soap.substitution",
                    "x-soap-substitution member without an element name",
                    location=loc))
                bad_member = True
            else:
                member_els.add(el)
        one_of = node.get("oneOf")
        if not isinstance(one_of, list) or not members:
            continue
        branch_props = set()
        bad_branch = False
        for branch in one_of:
            props = branch.get("properties") if isinstance(branch, dict) \
                else None
            if not (isinstance(props, dict) and len(props) == 1
                    and branch.get("required") == list(props)):
                out.append(_finding(
                    "x-soap.substitution",
                    "oneOf branch is not a self-describing "
                    "single-property object", location=loc))
                bad_branch = True
            else:
                branch_props.add(next(iter(props)))
        if not bad_branch and not bad_member and branch_props != member_els:
            out.append(_finding(
                "x-soap.substitution",
                f"oneOf branches {sorted(branch_props, key=repr)} do not "
                f"match substitution members "
                f"{sorted(member_els, key=repr)}", location=loc))
    return out


def _valid_choice_member(m: Any, names: set) -> bool:
    """A members[] entry is a bare property name (one branch), or a
    non-empty list of property names — a branch that is itself a
    bundled xsd:sequence of 2+ elements (#141 B2)."""
    if isinstance(m, str):
        return m in names
    if isinstance(m, list) and m:
        return all(isinstance(n, str) and n in names for n in m)
    return False


def _check_xsoap_choice(spec):
    # actual emitted/consumed shape (schema.py's _choice_groups,
    # bridge.py's _choice_violations): x-soap-choice is a list of
    # {"members": [...], "required": bool} group dicts. Each members[]
    # entry is either a bare property name, or a list of property names
    # for a branch that bundles 2+ elements together.
    out = []
    for loc, node in _iter_schema_nodes(spec):
        groups = node.get("x-soap-choice")
        if not isinstance(groups, list):
            continue
        props = node.get("properties")
        names = set(props) if isinstance(props, dict) else set()
        for group in groups:
            if not isinstance(group, dict):
                out.append(_finding(
                    "x-soap.choice",
                    "x-soap-choice group is not an object", location=loc))
                continue
            members = group.get("members")
            if not isinstance(members, list):
                out.append(_finding(
                    "x-soap.choice",
                    "x-soap-choice group without a members list",
                    location=loc))
                continue
            unknown = [m for m in members
                       if not _valid_choice_member(m, names)]
            if unknown:
                out.append(_finding(
                    "x-soap.choice",
                    f"x-soap-choice references unknown properties: "
                    f"{unknown}", location=loc))
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
    ("document.openapi3", _check_openapi3),
    ("tool-name.normalized", _check_tool_name_normalized),
    ("tool-description.present", _check_tool_description),
    ("x-soap.version", _check_xsoap_version),
    ("x-soap.output-element", _check_xsoap_output_element),
    ("x-soap.endpoint", _check_xsoap_endpoint),
    ("x-soap.mixed-rest", _check_xsoap_mixed_rest),
    ("x-soap.refs", _check_xsoap_refs),
    ("x-soap.substitution", _check_xsoap_substitution),
    ("x-soap.choice", _check_xsoap_choice),
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
            # own safety net (independent of verify()'s): a crash on an
            # input that previously crashed becomes one problem message
            # instead of propagating, without touching the frozen
            # messages of inputs that already worked.
            try:
                found = fn(spec)
            except Exception as exc:
                out.append(f"check could not complete: "
                           f"{type(exc).__name__}: {exc}")
                continue
            out.extend(r.message for r in found if r.status == "fail")
    return out


_DEEP_IDS = ("openapi.schema-valid", "fastmcp.roundtrip",
             "fastmcp.tool-materialized")


def verify(spec: Any, *, deep: bool = True) -> VerifyReport:
    """Run every check over an OpenAPI document; never raises.

    Returns a deterministic report in which every check id appears at
    least once (pass/warn/fail/skip) — except a non-mapping input, which
    yields the single document.mapping failure."""
    if not isinstance(spec, dict):
        return VerifyReport((_result(
            "document.mapping", "fail",
            "not an OpenAPI document (expected a mapping)"),))
    results: list[CheckResult] = [_result("document.mapping", "pass")]
    for cid, fn in _STATIC_CHECKS:
        try:
            found = fn(spec)
        except Exception as exc:
            # conformance's untestable policy: a check that cannot
            # complete is a visible fail, not a silently swallowed skip.
            found = [_result(cid, "fail", "check could not complete: "
                             f"{type(exc).__name__}: {exc}")]
        if found:
            results.extend(found)
        else:
            results.append(_result(cid, "pass"))
    results.extend(_run_deep_checks(spec, deep))
    results.sort(key=lambda r: (r.id, r.location))
    return VerifyReport(tuple(results))


def _run_deep_checks(spec: dict, deep: bool) -> list[CheckResult]:
    if not deep:
        return [_result(cid, "skip", "deep=False") for cid in _DEEP_IDS]
    return _run_openapi_validator(spec) + _run_fastmcp_roundtrip(spec)


def _run_openapi_validator(spec: dict) -> list[CheckResult]:
    try:
        from openapi_spec_validator import validate as osv_validate
    except ImportError:
        return [_result("openapi.schema-valid", "skip",
                        "openapi-spec-validator not installed")]
    try:
        osv_validate(spec)
    except Exception as exc:
        return [_result("openapi.schema-valid", "fail",
                        f"openapi-spec-validator: {exc}")]
    return [_result("openapi.schema-valid", "pass")]


def _run_fastmcp_roundtrip(spec: dict) -> list[CheckResult]:
    try:
        import anyio
        import httpx
        from fastmcp import Client, FastMCP
    except ImportError:
        msg = f"fastmcp not installed ({MCP_HINT})"
        return [_result("fastmcp.roundtrip", "skip", msg),
                _result("fastmcp.tool-materialized", "skip", msg)]

    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # anyio.run() cannot start a second event loop inside one that
        # is already running; without this guard the round-trip crashes
        # and is misreported as a "fail" rather than what it actually is
        # (untestable from this calling context).
        msg = ("cannot run the FastMCP round-trip inside a running event "
               "loop; call verify() from a synchronous context")
        return [_result("fastmcp.roundtrip", "skip", msg),
                _result("fastmcp.tool-materialized", "skip", msg)]

    # supply a dummy client so specs without a `servers` entry still
    # convert — verify measures tool convertibility, not deployment
    dummy = httpx.AsyncClient(base_url="http://spec2openapi.invalid")
    try:
        mcp = FastMCP.from_openapi(openapi_spec=spec, name="verify",
                                   client=dummy)

        async def _tools():
            try:
                async with Client(mcp) as client:
                    return await client.list_tools()
            finally:
                await dummy.aclose()

        tools = anyio.run(_tools)
    except Exception as exc:
        # if from_openapi() (or anything else before _tools() starts)
        # raised, _tools()'s finally never ran and dummy is still open;
        # best-effort close — verify() must never raise even if this does
        if not dummy.is_closed:
            try:
                anyio.run(dummy.aclose)
            except Exception:
                pass
        return [_result("fastmcp.roundtrip", "fail",
                        f"FastMCP round-trip failed: {exc}"),
                _result("fastmcp.tool-materialized", "skip",
                        "round-trip failed")]
    data = {"tools": [
        {"name": t.name,
         "params": list(((getattr(t, "inputSchema", None) or {})
                         .get("properties") or {}))}
        for t in sorted(tools, key=lambda t: t.name)]}
    results = [_result("fastmcp.roundtrip", "pass", data=data)]
    # FastMCP's actual tool-name normalization is version-specific
    # (fastmcp 3.4.7: '__'-splitting, [\s.-] run-collapsing, 64-char
    # truncation) and cannot be reliably predicted here, so materialization
    # is judged by count, not by matching predicted tool names.
    op_ids = [op.get("operationId") for _, _, op in _operations(spec)
              if isinstance(op.get("operationId"), str)]
    if len(tools) < len(op_ids):
        results.append(_result(
            "fastmcp.tool-materialized", "fail",
            f"operations not materialized as tools: expected "
            f"{len(op_ids)}, materialized {len(tools)}"))
    else:
        results.append(_result("fastmcp.tool-materialized", "pass"))
    return results
