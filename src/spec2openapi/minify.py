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
are summarized under ``x-s2o.minify`` (counts, key names, and per-op
fold state — never content); re-runs detect earlier folds through that
record, so they are never duplicated.
"""
from __future__ import annotations

import copy
import datetime
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

# Schema keywords whose values are subschemas (3.0 and 3.1 / JSON Schema
# 2020-12). "xml" is deliberately absent: the xml annotation object is
# opaque (it carries x-text, which the bridge reads to serialize
# xsd:simpleContent), and data keywords (example/examples/default/enum/
# const) hold data, not schemas.
_SUBSCHEMA_MAPS = ("properties", "patternProperties", "$defs",
                   "dependentSchemas")
_SUBSCHEMA_ONE = ("items", "additionalProperties", "not", "contains",
                  "propertyNames", "if", "then", "else",
                  "unevaluatedProperties", "unevaluatedItems")
_SUBSCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")

_ELLIPSIS = "…"
_EXAMPLE_FOLD_CAP = 300   # chars of rendered JSON per folded example line
_ERROR_DESC_CAP = 80      # chars per folded error-description fragment
_ERRORS_MARKER = "Errors: "
_REQUEST_EXAMPLE_MARKER = "Example request: "
_RESPONSE_EXAMPLE_MARKER = "Example response: "
_ALL_MARKERS = (_ERRORS_MARKER, _REQUEST_EXAMPLE_MARKER,
                _RESPONSE_EXAMPLE_MARKER)
# fold categories recorded per operation under x-s2o.minify.folded
_CAT_ERRORS = "errors"
_CAT_REQUEST = "request-example"
_CAT_RESPONSE = "response-example"

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
    prefix_tuple = tuple(prefixes)
    return lambda key: key in exact or key.startswith(prefix_tuple)


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


def _clean_schema(node: Any, keep: Callable[[str], bool], seen: set[int], *,
                  drop_ext: bool, drop_examples: bool,
                  max_desc: int | None, report: _Report) -> None:
    # `seen` guards cyclic schemas (YAML aliases survive deepcopy) and
    # keeps aliased subtrees from being counted twice
    if not isinstance(node, dict) or id(node) in seen:
        return
    seen.add(id(node))
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
                _clean_schema(child, keep, seen, drop_ext=drop_ext,
                              drop_examples=drop_examples,
                              max_desc=max_desc, report=report)
    for key in _SUBSCHEMA_ONE:
        child = node.get(key)
        if isinstance(child, dict):
            _clean_schema(child, keep, seen, drop_ext=drop_ext,
                          drop_examples=drop_examples,
                          max_desc=max_desc, report=report)
    for key in _SUBSCHEMA_LISTS:
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                _clean_schema(child, keep, seen, drop_ext=drop_ext,
                              drop_examples=drop_examples,
                              max_desc=max_desc, report=report)


def _media_schemas(container: Any):
    """Yield schema nodes under a 'content' map (requestBody, response,
    or a content-style parameter)."""
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


def _json_default(value: Any) -> str:
    # unquoted YAML timestamps load as date/datetime; their isoformat is
    # a faithful rendering of what the document said, not an invention
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def _render_example(value: Any, marker: str, ctx: str,
                    report: _Report) -> str | None:
    try:
        # allow_nan=False: NaN/Infinity are not valid JSON and would fold
        # as tokens no model should imitate — skip them via the except
        text = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          default=_json_default)
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


def _fold_errors(op: dict[str, Any], spec: dict[str, Any]) -> str | None:
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
            if len(first) > _ERROR_DESC_CAP:  # one long response
                first = _truncate(first, _ERROR_DESC_CAP)  # cannot blow
        if c == "default" and not first:      # the whole line's budget
            continue  # a bare "default" carries no information
        parts.append(f"{c} ({first})" if first else c)
    if not parts:
        return None
    return _ERRORS_MARKER + "; ".join(parts) + "."


def _split_folds(description: str) -> tuple[str, list[str]]:
    """Separate trailing fold lines (from a previous run) from the base.

    Callers gate this on the x-s2o.minify.folded record, so a marker-
    shaped line a *user* wrote is never misclassified as ours."""
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
    # write into a shallow copy, not the dict itself: an inline schema
    # shared through YAML anchors must not gain this parameter's example
    # at its other referencing sites
    param["schema"] = {**schema, "example": param.pop("example")}
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
    in-schema — and the ``summary`` of an operation without a
    description, since that is what FastMCP serves then).
    ``drop_value_examples`` removes in-schema examples. ``enrich`` folds
    invisible facts into descriptions: ``"errors"`` (error responses)
    and ``"examples"`` (payload examples, plus hoisting parameter
    examples into their schemas). See the module docstring for the exact
    contract.
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
    if max_description is not None and (
            isinstance(max_description, bool)
            or not isinstance(max_description, int)
            or max_description < 1):
        raise ConversionError(
            f"max_description must be an int >= 1, got {max_description!r}"
        )
    if isinstance(enrich, str):  # a lone "errors" is an obvious intent
        enrich = (enrich,)
    enrich = tuple(enrich)  # a generator must survive repeated `in` tests
    unknown = [c for c in enrich if c not in _ENRICH_CATEGORIES]
    if unknown:
        raise ConversionError(
            f"unknown enrich category {unknown[0]!r}: valid categories are "
            + ", ".join(sorted(_ENRICH_CATEGORIES))
        )
    if isinstance(keep_extensions, str):  # same courtesy as enrich
        keep_extensions = (keep_extensions,)

    out = copy.deepcopy(spec)
    report = _Report()
    keep = _keep_fn(keep_extensions)
    seen_schemas: set[int] = set()

    # Folds are detected through the x-s2o.minify.folded record, which
    # needs a writable (mapping) root x-s2o. A document whose x-s2o is
    # some other value is left alone: enrichment is skipped rather than
    # applied unrecordably (re-runs could not detect the folds).
    root_s2o = out.get("x-s2o")
    can_record = root_s2o is None or isinstance(root_s2o, dict)
    prior = root_s2o.get("minify") if isinstance(root_s2o, dict) else None
    prior = prior if isinstance(prior, dict) else {}
    folded_map: dict[str, list[str]] = {
        str(k): [str(c) for c in v]
        for k, v in prior.get("folded", {}).items()
        if isinstance(v, list)
    } if isinstance(prior.get("folded"), dict) else {}
    do_enrich = bool(enrich) and can_record
    if enrich and not can_record:
        report.notes.append(
            "root x-s2o is not an object; enrichment skipped "
            "(folds could not be recorded for idempotent re-runs)")

    def clean(node: Any) -> None:
        _clean_schema(node, keep, seen_schemas,
                      drop_ext=drop_foreign_extensions,
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
            for schema in _media_schemas(param):  # content-style params
                clean(schema)
            if max_description is not None:
                desc = param.get("description")
                if isinstance(desc, str) and len(desc) > max_description:
                    param["description"] = _truncate(desc, max_description)
                    report.bump("truncatedDescriptions")
            if do_enrich and "examples" in enrich:
                name = param.get("name", "?")
                _hoist_parameter_example(
                    param, drop_value_examples=drop_value_examples,
                    ctx=f"{ctx} parameter '{name}'", report=report)

    def process_operation(path: str, method: str, op: dict[str, Any]) -> None:
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

        op_key = str(op.get("operationId") or ctx)
        done = set(folded_map.get(op_key, ()))
        raw = op.get("description")
        original = raw if isinstance(raw, str) else ""
        # trailing fold lines belong to us only if the record says this
        # operation was folded before
        if done:
            base, previous_folds = _split_folds(original)
        else:
            base, previous_folds = original, []

        folds: list[str] = []
        newly_done: list[str] = []
        if do_enrich and "errors" in enrich and _CAT_ERRORS not in done:
            line = _fold_errors(op, out)
            if line:
                folds.append(line)
                newly_done.append(_CAT_ERRORS)
                report.bump("foldedErrorLines")
        if do_enrich and "examples" in enrich:
            if _CAT_REQUEST not in done:
                source = request_body
                if isinstance(source, dict) and "$ref" in source:
                    source = _resolve(out, source["$ref"])
                media = _first_json_media(source)
                if media is not None:
                    value, found = _media_example(media, out)
                    if found:
                        line = _render_example(
                            value, _REQUEST_EXAMPLE_MARKER,
                            f"{ctx} request", report)
                        if line:
                            folds.append(line)
                            newly_done.append(_CAT_REQUEST)
                            report.bump("foldedExampleLines")
            if _CAT_RESPONSE not in done and isinstance(responses, dict):
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
                        newly_done.append(_CAT_RESPONSE)
                        report.bump("foldedExampleLines")
                        break
                    # rendering failed (cap / not serializable): keep
                    # scanning later 2xx responses for a foldable one

        if folds and not base:
            # measured: FastMCP uses summary only as a fallback when
            # description is absent — starting the new description with
            # the summary keeps it in the payload
            summary = op.get("summary")
            base = summary if isinstance(summary, str) else ""
        # truncate the base BEFORE assembling folds (fold lines are never
        # truncated); doing this after the summary promotion is what
        # keeps minify(minify(x)) == minify(x)
        if max_description is not None and len(base) > max_description:
            base = _truncate(base, max_description)
            report.bump("truncatedDescriptions")

        final = "\n".join(p for p in (base, *previous_folds, *folds) if p)
        if folds:
            op["description"] = final
            folded_map[op_key] = sorted(done | set(newly_done))
        elif isinstance(raw, str) and final != raw:
            op["description"] = final  # truncation changed the base
        # an operation with no description serves its summary as the tool
        # description — that is the leaking channel then, so cap it too
        if (max_description is not None and not op.get("description")):
            summary = op.get("summary")
            if isinstance(summary, str) and len(summary) > max_description:
                op["summary"] = _truncate(summary, max_description)
                report.bump("truncatedDescriptions")

    # --- paths ---------------------------------------------------------
    paths = out.get("paths")
    if isinstance(paths, dict):
        from .openapi import _operations

        for path, item in paths.items():
            if isinstance(item, dict) and "$ref" not in item:
                handle_parameters(item.get("parameters"), path)
        for path, method, op in _operations(out):
            process_operation(path, method, op)

    # --- components ----------------------------------------------------
    components = out.get("components")
    if isinstance(components, dict):
        from .openapi import _HTTP_METHODS

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
        path_items = components.get("pathItems")  # OpenAPI 3.1
        if isinstance(path_items, dict):
            for name, item in path_items.items():
                if not isinstance(item, dict) or "$ref" in item:
                    continue
                item_ctx = f"components.pathItems.{name}"
                handle_parameters(item.get("parameters"), item_ctx)
                for method, op in item.items():
                    if (str(method).lower() in _HTTP_METHODS
                            and isinstance(op, dict)):
                        process_operation(item_ctx, method, op)

    # --- record --------------------------------------------------------
    # Written only when the run actually changed something (a no-op run
    # must return an equal document); merged with a previous record so a
    # chained run with different options keeps earlier fold state (which
    # re-run detection depends on) and the audit trail.
    if can_record and (report.changed
                       or (report.notes and "minify" not in
                           (root_s2o or {}))):
        block: dict[str, Any] = {}
        counts = dict(prior.get("counts")) \
            if isinstance(prior.get("counts"), dict) else {}
        for key, value in report.counts.items():
            if value:
                counts[key] = counts.get(key, 0) + value
        if counts:
            block["counts"] = counts
        keys = [str(k) for k in prior.get("droppedExtensionKeys", [])] \
            if isinstance(prior.get("droppedExtensionKeys"), list) else []
        keys += [k for k in report.dropped_keys if k not in keys]
        if keys:
            block["droppedExtensionKeys"] = keys
        if folded_map:
            block["folded"] = folded_map
        notes = [str(n) for n in prior.get("notes", [])] \
            if isinstance(prior.get("notes"), list) else []
        notes += [n for n in report.notes if n not in notes]
        if notes:
            block["notes"] = notes
        out.setdefault("x-s2o", {})["minify"] = block
    return out
