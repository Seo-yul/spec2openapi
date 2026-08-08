"""Optional post-processing: shrink and enrich the LLM-facing MCP surface.

When a spec is served through FastMCP, the model never reads the OpenAPI
document — it sees the tool list (name, description, inputSchema,
outputSchema). Measured against that channel:

- schema subtrees are copied verbatim into tool schemas, so foreign
  vendor ``x-*`` extensions there are dead weight (operation-level
  extensions never reach the payload and are left alone);
- error responses and media-type examples never reach the payload, and
  parameter-level ``example`` values are dropped while in-schema ones
  survive.

``minify_for_mcp`` removes the dead weight and, with ``enrich``, makes
the invisible facts visible: error responses and payload examples are
folded into the operation description as deterministic template lines,
and parameter examples are hoisted into the parameter schema. Only facts
already present in the document are used — nothing is invented.

Contract: given a valid OpenAPI 3.x input that passes
``check_fastmcp_ready``, the output is still valid and still passes; no
option touches the callable surface, so serving behavior (including SOAP
bridge envelopes) is unchanged. The input is not mutated, ``properties``
order is preserved, and the function is deterministic and idempotent —
a run that changes nothing returns an equal document. Removals and folds
are summarized under ``x-s2o.minify`` (counts and key names, never
content).
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Iterable

from .errors import ConversionError

# --- extension keep lists ---------------------------------------------------
# Runtime-critical: what the SOAP bridge / FastMCP read at serve time.
_KEEP_EXACT_RUNTIME = frozenset({"x-soap"})
_KEEP_PREFIXES_RUNTIME = ("x-soap-", "x-fastmcp-")
# Project-emitted: everything our own converters write must survive their
# own minifier (x-pattern/x-collectionFormat are lossy-preservation
# companions recorded in x-s2o.lossy; deleting them would orphan those
# records and break the "untranslatable syntax is preserved" invariant).
_KEEP_EXACT_PROJECT = frozenset({
    "x-s2o", "x-pattern", "x-collectionFormat", "x-original-body-name",
})
# Documentation-bearing community extensions: caller-facing hints a model
# can use — exactly the kind of compact signal this function preserves.
_KEEP_EXACT_DOC = frozenset({
    "x-enum-varnames", "x-enum-descriptions",
    "x-example", "x-examples", "x-deprecated-reason",
})

# Schema keywords whose values are subschemas. "xml" is deliberately
# absent: the xml annotation object is opaque (it carries x-text, which
# the bridge reads to serialize xsd:simpleContent), and data keywords
# (example/examples/default/enum/const) hold data, not schemas.
_SUBSCHEMA_MAPS = ("properties", "patternProperties")
_SUBSCHEMA_ONE = ("items", "additionalProperties", "not", "contains",
                  "propertyNames")
_SUBSCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")

_ELLIPSIS = "…"
_EXAMPLE_FOLD_CAP = 300  # chars of rendered JSON per folded example line
_ERRORS_MARKER = "Errors: "
_REQUEST_EXAMPLE_MARKER = "Example request: "
_RESPONSE_EXAMPLE_MARKER = "Example response: "
_ALL_MARKERS = (_ERRORS_MARKER, _REQUEST_EXAMPLE_MARKER,
                _RESPONSE_EXAMPLE_MARKER)

_ENRICH_CATEGORIES = frozenset({"errors", "examples"})
_ERROR_RANGE_RE = re.compile(r"[45]XX", re.IGNORECASE)
_SUCCESS_RE = re.compile(r"2\d\d|2XX", re.IGNORECASE)


class _Report:
    """Accumulates what a run removed/folded, in first-seen order."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.dropped_keys: list[str] = []
        self._seen_keys: set[str] = set()
        self.notes: list[str] = []

    def bump(self, category: str, n: int = 1) -> None:
        self.counts[category] = self.counts.get(category, 0) + n

    def drop_key(self, name: str) -> None:
        if name not in self._seen_keys:
            self._seen_keys.add(name)
            self.dropped_keys.append(name)
        self.bump("droppedExtensions")

    @property
    def changed(self) -> bool:
        return any(self.counts.values())


def _keep_fn(keep_extensions: Iterable[str]) -> Callable[[str], bool]:
    exact = set(_KEEP_EXACT_RUNTIME) | _KEEP_EXACT_PROJECT | _KEEP_EXACT_DOC
    prefixes = list(_KEEP_PREFIXES_RUNTIME)
    for entry in keep_extensions:
        entry = str(entry)
        if entry.endswith("*"):
            prefixes.append(entry[:-1])
        else:
            exact.add(entry)
    return lambda key: key in exact or key.startswith(tuple(prefixes))


def _resolve(spec: dict[str, Any], ref: Any) -> Any:
    """Resolve an internal '#/...' JSON pointer; None if unresolvable."""
    if not (isinstance(ref, str) and ref.startswith("#/")):
        return None
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _truncate(text: str, cap: int) -> str:
    # The result including the ellipsis fits within cap, so a re-run
    # sees len <= cap and does nothing (idempotency).
    return text[: cap - 1].rstrip() + _ELLIPSIS


def _clean_schema(node: Any, keep: Callable[[str], bool], *,
                  drop_ext: bool, drop_examples: bool,
                  max_desc: int | None, report: _Report) -> None:
    if not isinstance(node, dict):
        return
    if drop_ext:
        for key in [k for k in node if k.startswith("x-") and not keep(k)]:
            del node[key]
            report.drop_key(key)
    if drop_examples:
        if "example" in node:
            del node["example"]
            report.bump("droppedExamples")
        # the 3.1 JSON Schema keyword is a list; media-type `examples`
        # maps are not schema nodes and are never walked here
        if isinstance(node.get("examples"), list):
            del node["examples"]
            report.bump("droppedExamples")
    if max_desc is not None:
        desc = node.get("description")
        if isinstance(desc, str) and len(desc) > max_desc:
            node["description"] = _truncate(desc, max_desc)
            report.bump("truncatedDescriptions")
    for key in _SUBSCHEMA_MAPS:
        sub = node.get(key)
        if isinstance(sub, dict):
            for child in sub.values():
                _clean_schema(child, keep, drop_ext=drop_ext,
                              drop_examples=drop_examples,
                              max_desc=max_desc, report=report)
    for key in _SUBSCHEMA_ONE:
        child = node.get(key)
        if isinstance(child, dict):
            _clean_schema(child, keep, drop_ext=drop_ext,
                          drop_examples=drop_examples,
                          max_desc=max_desc, report=report)
    for key in _SUBSCHEMA_LISTS:
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                _clean_schema(child, keep, drop_ext=drop_ext,
                              drop_examples=drop_examples,
                              max_desc=max_desc, report=report)


def _media_schemas(container: Any):
    """Yield schema nodes under a requestBody/response 'content' map."""
    if not isinstance(container, dict):
        return
    content = container.get("content")
    if not isinstance(content, dict):
        return
    for media in content.values():
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            yield media["schema"]


def _first_json_media(container: Any) -> dict[str, Any] | None:
    """First JSON media object (subtype json or +json) in document order."""
    if not isinstance(container, dict):
        return None
    content = container.get("content")
    if not isinstance(content, dict):
        return None
    for mt, media in content.items():
        base = str(mt).split(";", 1)[0].strip().lower()
        subtype = base.partition("/")[2]
        if (subtype == "json" or subtype.endswith("+json")) \
                and isinstance(media, dict):
            return media
    return None


def _media_example(media: dict[str, Any], spec: dict[str, Any]) -> tuple[Any, bool]:
    """(value, found) from a media object's example/examples."""
    if "example" in media:
        return media["example"], True
    examples = media.get("examples")
    if isinstance(examples, dict):
        for entry in examples.values():
            if isinstance(entry, dict) and "$ref" in entry:
                entry = _resolve(spec, entry["$ref"])
            if isinstance(entry, dict) and "value" in entry:
                return entry["value"], True
            # entries carrying only externalValue have no inline value
    return None, False


def _render_example(value: Any, marker: str, ctx: str,
                    report: _Report) -> str | None:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        report.notes.append(f"{ctx}: example is not JSON-serializable; "
                            "not folded")
        return None
    if len(text) > _EXAMPLE_FOLD_CAP:
        report.notes.append(
            f"{ctx}: example exceeds the {_EXAMPLE_FOLD_CAP}-char fold cap "
            f"({len(text)} chars); not folded")
        return None
    return marker + text


def _fold_errors(op: dict[str, Any], spec: dict[str, Any],
                 ctx: str) -> str | None:
    responses = op.get("responses")
    if not isinstance(responses, dict):
        return None
    # on x-soap operations the generator's structural fault response (the
    # synthetic 500) is skipped: build_spec already writes the
    # "Declared faults: ..." doc line
    skip_500 = isinstance(op.get("x-soap"), dict)
    parts: list[str] = []
    for code, resp in responses.items():
        c = str(code)
        if skip_500 and c == "500":
            continue
        is_error = (_ERROR_RANGE_RE.fullmatch(c)
                    or (c.isdigit() and int(c) >= 400))
        if not (is_error or c == "default"):
            continue
        if isinstance(resp, dict) and "$ref" in resp:
            resp = _resolve(spec, resp["$ref"])
        desc = resp.get("description") if isinstance(resp, dict) else None
        first = ""
        if isinstance(desc, str) and desc.strip():
            first = desc.strip().splitlines()[0].strip()
        if c == "default" and not first:
            continue  # a bare "default" carries no information
        parts.append(f"{c} ({first})" if first else c)
    if not parts:
        return None
    return _ERRORS_MARKER + "; ".join(parts) + "."


def _split_folds(description: str) -> tuple[str, list[str]]:
    """Separate trailing fold lines (from a previous run) from the base."""
    lines = description.split("\n")
    folds: list[str] = []
    while lines and lines[-1].startswith(_ALL_MARKERS):
        folds.insert(0, lines.pop())
    return "\n".join(lines), folds


def _hoist_parameter_example(param: Any, *, drop_value_examples: bool,
                             ctx: str, report: _Report) -> None:
    if not isinstance(param, dict) or "$ref" in param or "example" not in param:
        return
    if drop_value_examples:
        report.notes.append(f"{ctx}: parameter example not hoisted "
                            "(drop_value_examples is enabled)")
        return
    schema = param.get("schema")
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        # hoisting into a shared component schema would inject this
        # parameter's example into every site that references it
        report.notes.append(f"{ctx}: parameter example not hoisted "
                            "(schema is a shared $ref)")
        return
    if "example" in schema or isinstance(schema.get("examples"), list):
        return  # the schema already shows its own example
    schema["example"] = param.pop("example")
    report.bump("hoistedParameterExamples")


def minify_for_mcp(
    spec: dict[str, Any],
    *,
    drop_foreign_extensions: bool = True,
    keep_extensions: tuple[str, ...] = (),
    max_description: int | None = None,
    drop_value_examples: bool = False,
    enrich: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a copy of an OpenAPI 3.x spec minified for MCP serving.

    Removes foreign vendor ``x-*`` extensions from schema subtrees
    (including inside ``components``); ``keep_extensions`` adds names to
    preserve (a trailing ``*`` matches as a prefix). ``max_description``
    caps the descriptions that reach tool payloads (operation, parameter,
    in-schema). ``drop_value_examples`` removes in-schema examples.
    ``enrich`` folds invisible facts into descriptions: ``"errors"``
    (error responses) and ``"examples"`` (payload examples, plus hoisting
    parameter examples into their schemas). See the module docstring for
    the exact contract.
    """
    if not isinstance(spec, dict):
        raise ConversionError(
            "minify_for_mcp expects an OpenAPI 3.x document (a mapping), "
            f"got {type(spec).__name__}"
        )
    from .swagger import is_swagger2

    if is_swagger2(spec):
        raise ConversionError(
            "input is Swagger 2.0; run convert_swagger first, then minify"
        )
    version = str(spec.get("openapi", ""))
    if not version.startswith("3."):
        raise ConversionError(
            "not an OpenAPI 3.x document "
            f"(openapi={spec.get('openapi')!r})"
        )
    if max_description is not None and max_description < 1:
        raise ConversionError(
            f"max_description must be >= 1, got {max_description!r}"
        )
    if isinstance(enrich, str):  # a lone "errors" is an obvious intent
        enrich = (enrich,)
    unknown = [c for c in enrich if c not in _ENRICH_CATEGORIES]
    if unknown:
        raise ConversionError(
            f"unknown enrich category {unknown[0]!r}: valid categories are "
            + ", ".join(sorted(_ENRICH_CATEGORIES))
        )

    out = copy.deepcopy(spec)
    report = _Report()
    keep = _keep_fn(keep_extensions)

    def clean(node: Any) -> None:
        _clean_schema(node, keep, drop_ext=drop_foreign_extensions,
                      drop_examples=drop_value_examples,
                      max_desc=max_description, report=report)

    def handle_parameters(params: Any, ctx: str) -> None:
        if not isinstance(params, list):
            return
        for param in params:
            if not isinstance(param, dict) or "$ref" in param:
                continue  # $ref targets are handled via components
            if isinstance(param.get("schema"), dict):
                clean(param["schema"])
            if max_description is not None:
                desc = param.get("description")
                if isinstance(desc, str) and len(desc) > max_description:
                    param["description"] = _truncate(desc, max_description)
                    report.bump("truncatedDescriptions")
            if "examples" in enrich:
                name = param.get("name", "?")
                _hoist_parameter_example(
                    param, drop_value_examples=drop_value_examples,
                    ctx=f"{ctx} parameter '{name}'", report=report)

    # --- paths ---------------------------------------------------------
    paths = out.get("paths")
    if isinstance(paths, dict):
        from .openapi import _operations

        for path, item in paths.items():
            if isinstance(item, dict):
                handle_parameters(item.get("parameters"), path)
        for path, method, op in _operations(out):
            ctx = f"{str(method).upper()} {path}"
            handle_parameters(op.get("parameters"), ctx)
            request_body = op.get("requestBody")
            if isinstance(request_body, dict) and "$ref" not in request_body:
                for schema in _media_schemas(request_body):
                    clean(schema)
            responses = op.get("responses")
            if isinstance(responses, dict):
                for resp in responses.values():
                    if isinstance(resp, dict) and "$ref" not in resp:
                        for schema in _media_schemas(resp):
                            clean(schema)

            # description: truncate the base (original text) only —
            # fold lines from a previous run are split off and re-appended
            # untouched, and new folds are appended after them
            base, previous_folds = _split_folds(op.get("description") or "")
            if (max_description is not None
                    and len(base) > max_description):
                base = _truncate(base, max_description)
                report.bump("truncatedDescriptions")

            existing = "\n".join([base, *previous_folds])
            folds: list[str] = []
            if "errors" in enrich and _ERRORS_MARKER not in existing:
                line = _fold_errors(op, out, ctx)
                if line:
                    folds.append(line)
                    report.bump("foldedErrorLines")
            if "examples" in enrich:
                if _REQUEST_EXAMPLE_MARKER not in existing:
                    request_body = op.get("requestBody")
                    if isinstance(request_body, dict) and "$ref" in request_body:
                        request_body = _resolve(out, request_body["$ref"])
                    media = _first_json_media(request_body)
                    if media is not None:
                        value, found = _media_example(media, out)
                        if found:
                            line = _render_example(
                                value, _REQUEST_EXAMPLE_MARKER,
                                f"{ctx} request", report)
                            if line:
                                folds.append(line)
                                report.bump("foldedExampleLines")
                if (_RESPONSE_EXAMPLE_MARKER not in existing
                        and isinstance(responses, dict)):
                    for code, resp in responses.items():
                        if not _SUCCESS_RE.fullmatch(str(code)):
                            continue
                        if isinstance(resp, dict) and "$ref" in resp:
                            resp = _resolve(out, resp["$ref"])
                        media = _first_json_media(resp)
                        if media is None:
                            continue
                        value, found = _media_example(media, out)
                        if not found:
                            continue
                        line = _render_example(
                            value, _RESPONSE_EXAMPLE_MARKER,
                            f"{ctx} response {code}", report)
                        if line:
                            folds.append(line)
                            report.bump("foldedExampleLines")
                        break
            if folds:
                if not base:
                    # measured: FastMCP uses summary only as a fallback
                    # when description is absent — starting the new
                    # description with the summary keeps it in the payload
                    base = str(op.get("summary") or "")
                pieces = [p for p in (base, *previous_folds, *folds) if p]
                op["description"] = "\n".join(pieces)
            elif op.get("description"):
                pieces = [p for p in (base, *previous_folds) if p]
                op["description"] = "\n".join(pieces)

    # --- components ----------------------------------------------------
    components = out.get("components")
    if isinstance(components, dict):
        schemas = components.get("schemas")
        if isinstance(schemas, dict):
            for schema in schemas.values():
                clean(schema)
        parameters = components.get("parameters")
        if isinstance(parameters, dict):
            handle_parameters(list(parameters.values()),
                              "components.parameters")
        for section in ("requestBodies", "responses"):
            entries = components.get(section)
            if isinstance(entries, dict):
                for entry in entries.values():
                    if isinstance(entry, dict) and "$ref" not in entry:
                        for schema in _media_schemas(entry):
                            clean(schema)

    # --- record --------------------------------------------------------
    # Written only when the run actually changed something (a no-op run
    # must return an equal document); a notes-only run records once.
    existing_s2o = out.get("x-s2o")
    has_record = isinstance(existing_s2o, dict) and "minify" in existing_s2o
    if report.changed or (report.notes and not has_record):
        block: dict[str, Any] = {}
        counts = {k: v for k, v in report.counts.items() if v}
        if counts:
            block["counts"] = counts
        if report.dropped_keys:
            block["droppedExtensionKeys"] = report.dropped_keys
        if report.notes:
            block["notes"] = report.notes
        out.setdefault("x-s2o", {})["minify"] = block
    return out
