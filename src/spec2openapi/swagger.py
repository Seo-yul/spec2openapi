"""Swagger 2.0 -> OpenAPI 3.x upgrade (FastMCP only accepts OpenAPI 3.x).

Design principles for information gaps (2.0 documents often omit things):

1. Apply industry-consensus defaults deterministically and RECORD every
   assumption under the root `x-s2o.assumptions` so the upgrade is
   auditable (missing consumes/produces -> application/json, missing
   operationId -> "{method}_{path}", missing host -> relative server "/").
2. Never silently drop untranslatable constructs: preserve them as `x-`
   extensions and record them under `x-s2o.lossy`.
3. The final gate is `spec2openapi validate`, whose FastMCP round-trip
   proves the upgraded spec actually materializes as MCP tools.
"""
from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any

from . import __version__ as _version
from .errors import ConversionError
from .openapi import _unique_id, to_openapi_31

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")

# schema keywords whose *values* are data, not sub-schemas — never rewrite
# $ref/x-nullable/discriminator inside them
_DATA_KEYWORDS = ("example", "examples", "default", "enum")

# Swagger 2.0 parameter fields that move into `schema` in OpenAPI 3
_SCHEMA_FIELDS = (
    "type", "format", "items", "default", "maximum", "exclusiveMaximum",
    "minimum", "exclusiveMinimum", "maxLength", "minLength", "pattern",
    "maxItems", "minItems", "uniqueItems", "enum", "multipleOf",
)

# FastMCP normalizes tool names to [A-Za-z0-9_]; generate ids accordingly
# so that tool name == operationId holds after the round-trip.
_ID_RE = re.compile(r"[^A-Za-z0-9_]+")
# OpenAPI 3 component keys allow letters, digits, '.', '-', '_'
_COMPONENT_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")


def is_swagger2(spec: dict[str, Any]) -> bool:
    # predicate: a non-mapping simply isn't a Swagger 2.0 document
    return isinstance(spec, dict) and str(spec.get("swagger", "")).startswith("2")


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _merge_params(shared: list, op_level: list) -> list:
    """Combine path-item and operation parameters. Per the spec, an
    operation parameter overrides a path-item one with the same
    (name, in). $ref params (no visible name/in) are kept as-is."""
    merged: list = []
    index: dict[tuple, int] = {}
    for p in list(shared or []) + list(op_level or []):
        if isinstance(p, dict) and "name" in p and "in" in p:
            key = (p["name"], p["in"])
            if key in index:
                merged[index[key]] = p  # later (op-level) wins
                continue
            index[key] = len(merged)
        merged.append(p)
    return merged


def convert_swagger(
    spec: dict[str, Any], *, openapi_version: str = "3.0",
    strict: bool = False,
) -> dict[str, Any]:
    """Upgrade a Swagger 2.0 dict to OpenAPI 3.0 (or 3.1).

    strict=True fails (ConversionError) when the conversion would need any
    assumption or lossy transformation — for pipelines that must not accept
    guessed conversions. The records are listed in the error message.
    """
    if not isinstance(spec, dict):
        raise ConversionError(
            "convert_swagger expects an OpenAPI/Swagger mapping (dict), "
            f"got {type(spec).__name__}"
        )
    if not is_swagger2(spec):
        raise ValueError("not a Swagger 2.0 document (missing swagger: '2.0')")
    up = _Upgrader(spec)
    out = up.convert()
    if strict and (up.assumptions or up.lossy):
        records = [f"assumption: {a}" for a in up.assumptions]
        records += [f"lossy: {m}" for m in up.lossy]
        shown = "\n  ".join(records[:20])
        more = f"\n  … and {len(records) - 20} more" if len(records) > 20 else ""
        raise ConversionError(
            f"strict mode: conversion required {len(records)} "
            f"assumption(s)/lossy transformation(s):\n  {shown}{more}"
        )
    if openapi_version.startswith("3.1"):
        out = to_openapi_31(out)
    return out


class _Upgrader:
    def __init__(self, src: dict[str, Any]):
        self.assumptions: list[str] = []
        self.lossy: list[str] = []
        # null values are invalid almost everywhere in OpenAPI 3; strip them
        # up front (x- extensions and example/default/enum data are kept)
        self._nulls_stripped = 0
        src = self._strip_nulls(src)
        if self._nulls_stripped:
            self.assumptions.append(
                f"removed {self._nulls_stripped} null value(s) (invalid in "
                "OpenAPI 3; x- extensions and example/default/enum data kept)"
            )
        self.src = src
        # global body-parameters become requestBodies, not parameters
        self._global_body_params: set[str] = {
            name
            for name, p in (src.get("parameters") or {}).items()
            if isinstance(p, dict) and p.get("in") == "body"
        }
        # OpenAPI 3 component keys must match ^[a-zA-Z0-9.\-_]+$; map each
        # definition name to a valid, unique component key
        self._schema_key: dict[str, str] = {}
        used: set[str] = set()
        for name in (src.get("definitions") or {}):
            key = _COMPONENT_KEY_RE.sub("_", name).strip("_") or "schema"
            if key in used:
                i = 2
                while f"{key}_{i}" in used:
                    i += 1
                key = f"{key}_{i}"
            used.add(key)
            self._schema_key[name] = key
            if key != name:
                self.assumptions.append(
                    f"definition '{name}' renamed to component schema "
                    f"'{key}' (OpenAPI 3 component-key charset)"
                )

    # -- helpers -----------------------------------------------------------

    def _strip_nulls(self, node: Any) -> Any:
        if isinstance(node, list):
            return [self._strip_nulls(v) for v in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if (isinstance(k, str) and k.startswith("x-")) or k in _DATA_KEYWORDS:
                out[k] = v  # null may be meaningful data here
                continue
            if v is None:
                self._nulls_stripped += 1
                continue
            out[k] = self._strip_nulls(v)
        return out

    def _fix_ref(self, ref: str) -> str:
        if ref.startswith("#/definitions/"):
            name = ref[len("#/definitions/"):]
            key = self._schema_key.get(name, name)
            return f"#/components/schemas/{key}"
        if ref.startswith("#/parameters/"):
            name = ref.rsplit("/", 1)[-1]
            if name in self._global_body_params:
                return f"#/components/requestBodies/{name}"
            return ref.replace("#/parameters/", "#/components/parameters/", 1)
        if ref.startswith("#/responses/"):
            return ref.replace("#/responses/", "#/components/responses/", 1)
        return ref

    def _fix_schema(self, node: Any) -> Any:
        """Recursive schema fixups: $refs, type:file, x-nullable,
        discriminator string -> object."""
        if isinstance(node, list):
            return [self._fix_schema(v) for v in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out[k] = self._fix_ref(v)
            elif k == "x-nullable":
                out["nullable"] = v
            elif k == "discriminator" and isinstance(v, str):
                out[k] = {"propertyName": v}
            elif k == "discriminator" and isinstance(v, dict):
                if v.get("propertyName"):
                    out[k] = v
                else:  # invalid: discriminator requires propertyName
                    self.lossy.append(
                        "discriminator without 'propertyName' dropped"
                    )
            elif k in _DATA_KEYWORDS:
                out[k] = v  # data value: pass through verbatim
            else:
                out[k] = self._fix_schema(v)
        if out.get("type") == "file":
            out["type"] = "string"
            out["format"] = "binary"
        elif out.get("type") == "array" and "items" not in out:
            # OpenAPI (3.0 and 3.1) requires items on an array schema
            out["items"] = {}
            self.assumptions.append(
                "an array schema without 'items' got 'items: {}' (required "
                "in OpenAPI 3)"
            )
        elif isinstance(out.get("type"), list):
            # JSON-Schema type array is invalid in OpenAPI 3.0; collapse to a
            # single type + nullable (to_openapi_31 re-expands for 3.1)
            types = [t for t in out["type"] if t != "null"]
            if "null" in out["type"]:
                out["nullable"] = True
            if len(types) == 1:
                out["type"] = types[0]
            elif not types:
                out.pop("type")
            else:
                out["type"] = types[0]
                self.lossy.append(
                    f"schema type {out.get('type')!r} chosen from multi-type "
                    f"array {node['type']} (OpenAPI 3.0 allows one type)"
                )
        if out.get("type") == "null":
            # JSON-Schema's 'null' type has no OAS 3.0 equivalent
            out.pop("type")
            out["nullable"] = True
            self.assumptions.append(
                "schema type 'null' converted to nullable: true"
            )
        if isinstance(out.get("items"), list):
            # tuple-style items array; OAS 3 requires a single schema
            items = out["items"]
            out["items"] = (items[0] if len(items) == 1
                            else {"anyOf": items} if items else {})
            self.assumptions.append(
                "tuple-style 'items' array collapsed to a single schema "
                "(OpenAPI 3 requires one items schema)"
            )
        props = out.get("properties")
        if isinstance(props, dict):
            # draft-4 style boolean `required` on a property: hoist the name
            # into the parent schema's required array
            hoisted = []
            for pname, pschema in props.items():
                if isinstance(pschema, dict) and isinstance(
                        pschema.get("required"), bool):
                    if pschema.pop("required"):
                        hoisted.append(pname)
            if hoisted:
                req = out.get("required")
                req = list(req) if isinstance(req, list) else []
                req.extend(n for n in hoisted if n not in req)
                out["required"] = req
                self.assumptions.append(
                    "boolean 'required' on properties hoisted to the parent "
                    "schema's required list"
                )
        return out

    def _media_types(self, kind: str, op: dict, ctx: str) -> list[str]:
        types = op.get(kind) or self.src.get(kind) or []
        if isinstance(types, str):
            # spec violation seen in the wild: a bare string instead of an
            # array — list(str) would split it into characters
            self.assumptions.append(
                f"{ctx}: '{kind}' was a string; wrapped in a list"
            )
            types = [types]
        if not types:
            self.assumptions.append(
                f"{ctx}: no '{kind}' declared; assumed application/json"
            )
            return ["application/json"]
        return [str(t) for t in types]

    # -- parameters --------------------------------------------------------

    def _convert_param(self, p: dict, ctx: str) -> dict:
        """query/path/header (non-body, non-formData) parameter."""
        loc = p.get("in")
        out = {
            k: v
            for k, v in p.items()
            if k in ("name", "in", "description")
            or k.startswith("x-")
        }
        # always emit an explicit boolean 'required' (absent == false in
        # OpenAPI 3, but we make it explicit for unambiguous output)
        out["required"] = _as_bool(p.get("required", False))
        # allowEmptyValue is valid only for query parameters in OpenAPI 3
        if "allowEmptyValue" in p:
            if loc == "query":
                out["allowEmptyValue"] = _as_bool(p["allowEmptyValue"])
            else:
                self.lossy.append(
                    f"{ctx}: allowEmptyValue on {loc or 'unknown'} parameter "
                    f"'{p.get('name')}' dropped (query-only in OpenAPI 3)"
                )
        # OpenAPI 3 requires path parameters to be required:true
        if loc == "path" and out["required"] is not True:
            out["required"] = True
            self.assumptions.append(
                f"{ctx}: path parameter '{p.get('name')}' forced to "
                "required:true (mandatory in OpenAPI 3)"
            )
        # run the assembled schema through _fix_schema so top-level rules
        # (type:file, array-needs-items, type-array) apply
        raw_schema = {k: v for k, v in p.items() if k in _SCHEMA_FIELDS}
        schema = self._fix_schema(raw_schema) if raw_schema else {}
        out["schema"] = schema or {"type": "string"}

        cf = p.get("collectionFormat")
        if cf:
            # style/explode depend on the location; path/header accept only
            # 'simple' (form/spaceDelimited/pipeDelimited are query-only)
            if loc in ("query", "formData"):
                mapping = {
                    "csv": ("form", False), "multi": ("form", True),
                    "ssv": ("spaceDelimited", False),
                    "pipes": ("pipeDelimited", False),
                }
                if cf in mapping:
                    out["style"], out["explode"] = mapping[cf]
                else:  # tsv has no OpenAPI 3 equivalent
                    out["x-collectionFormat"] = cf
                    self.lossy.append(
                        f"{ctx}: collectionFormat '{cf}' has no OpenAPI 3 "
                        "equivalent; preserved as x-collectionFormat"
                    )
            else:  # path / header
                out["style"], out["explode"] = "simple", False
                if cf != "csv":
                    out["x-collectionFormat"] = cf
                    self.lossy.append(
                        f"{ctx}: collectionFormat '{cf}' on {loc} parameter "
                        "has no OpenAPI 3 equivalent; used style:simple and "
                        "preserved x-collectionFormat"
                    )
        return out

    def _body_to_request_body(self, p: dict, op: dict, ctx: str) -> dict:
        rb: dict[str, Any] = {}
        if p.get("description"):
            rb["description"] = p["description"]
        if p.get("required"):
            rb["required"] = True
        if p.get("name"):
            rb["x-original-body-name"] = p["name"]
        schema = self._fix_schema(p.get("schema", {}))
        rb["content"] = {
            mt: {"schema": schema}
            for mt in self._media_types("consumes", op, ctx)
        }
        return rb

    def _form_to_request_body(self, params: list[dict], op: dict,
                              ctx: str) -> dict:
        has_file = any(p.get("type") == "file" for p in params)
        media = "multipart/form-data" if has_file else (
            "application/x-www-form-urlencoded"
        )
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in params:
            name = p.get("name")
            if not name:  # formData property name is mandatory
                self.lossy.append(
                    f"{ctx}: formData parameter without a name dropped"
                )
                continue
            raw_schema = {k: v for k, v in p.items() if k in _SCHEMA_FIELDS}
            schema = self._fix_schema(raw_schema) if raw_schema else {}
            if p.get("description"):
                schema["description"] = p["description"]
            props[name] = schema or {"type": "string"}
            if p.get("required"):
                required.append(name)
            if p.get("collectionFormat"):
                self.lossy.append(
                    f"{ctx}: collectionFormat '{p['collectionFormat']}' on "
                    f"formData '{p.get('name')}' dropped (no requestBody "
                    "encoding equivalent emitted)"
                )
        body_schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            body_schema["required"] = required
        return {
            "required": bool(required),
            "content": {media: {"schema": body_schema}},
        }

    def _split_params(self, raw: list, op: dict, ctx: str
                      ) -> tuple[list, dict | None]:
        """-> (parameters, requestBody|None)"""
        params: list[dict] = []
        body: dict | None = None
        form: list[dict] = []
        for p in raw:
            if not isinstance(p, dict):
                self.lossy.append(
                    f"{ctx}: a non-object parameter entry was dropped"
                )
                continue
            if "$ref" in p:
                ref = self._fix_ref(p["$ref"])
                if "/requestBodies/" in ref:
                    body = {"$ref": ref}
                    continue
                # a $ref to a global formData parameter must be inlined:
                # formData fields are fragments of the form requestBody and
                # have no standalone OpenAPI 3 component equivalent
                gname = p["$ref"].rsplit("/", 1)[-1]
                gparam = (self.src.get("parameters") or {}).get(gname)
                if isinstance(gparam, dict) and gparam.get("in") == "formData":
                    form.append(gparam)
                    self.assumptions.append(
                        f"{ctx}: $ref to global formData parameter "
                        f"'{gname}' inlined into the form requestBody"
                    )
                    continue
                params.append({"$ref": ref})
                continue
            loc = p.get("in")
            if loc == "body":
                if body is not None:
                    self.lossy.append(
                        f"{ctx}: multiple body parameters; kept the last "
                        "(OpenAPI 3 allows only one requestBody)"
                    )
                body = self._body_to_request_body(p, op, ctx)
            elif loc == "formData":
                form.append(p)
            elif not p.get("name"):
                # query/path/header parameters require a name in OpenAPI 3
                self.lossy.append(
                    f"{ctx}: {loc or 'unknown-location'} parameter without a "
                    "name dropped"
                )
            else:
                params.append(self._convert_param(p, ctx))
        if form:
            if body is not None:
                self.lossy.append(
                    f"{ctx}: both body and formData parameters present; "
                    "formData ignored"
                )
            else:
                body = self._form_to_request_body(form, op, ctx)
        return params, body

    # -- responses ----------------------------------------------------------

    @staticmethod
    def _status_phrase(code: Any) -> str:
        """Deterministic default description for a status code."""
        if code == "default":
            return "Default response"
        try:
            return HTTPStatus(int(code)).phrase
        except (ValueError, TypeError):
            return ""

    def _convert_response(self, resp: dict, op: dict, ctx: str,
                          code: Any = None) -> dict:
        if "$ref" in resp:
            return {"$ref": self._fix_ref(resp["$ref"])}
        description = resp.get("description")
        if not description:  # required on the OA3 Response Object
            description = self._status_phrase(code)
            filled = f"'{description}'" if description else "an empty string"
            self.assumptions.append(
                f"{ctx}: response missing 'description' (required in "
                f"OpenAPI 3); used {filled}"
            )
        out: dict[str, Any] = {"description": description}
        for k, v in resp.items():
            if k.startswith("x-"):
                out[k] = v
        if "schema" in resp:
            schema = self._fix_schema(resp["schema"])
            examples = resp.get("examples") or {}
            content: dict[str, Any] = {}
            for mt in self._media_types("produces", op, ctx):
                entry: dict[str, Any] = {"schema": schema}
                if mt in examples:
                    entry["example"] = examples[mt]
                content[mt] = entry
            out["content"] = content
        if "headers" in resp:
            headers = {}
            for hname, h in resp["headers"].items():
                hh = {k: v for k, v in h.items() if k == "description"}
                hh["schema"] = {
                    k: self._fix_schema(v) for k, v in h.items()
                    if k in _SCHEMA_FIELDS
                } or {"type": "string"}
                headers[hname] = hh
            out["headers"] = headers
        return out

    # -- security -------------------------------------------------------------

    # oauth2 flow name mapping + which URLs each flow requires in OpenAPI 3
    _OAUTH_FLOWS = {
        "implicit": ("implicit", ("authorizationUrl",)),
        "password": ("password", ("tokenUrl",)),
        "application": ("clientCredentials", ("tokenUrl",)),
        "accessCode": ("authorizationCode", ("authorizationUrl", "tokenUrl")),
    }

    def _convert_security_schemes(self) -> dict:
        out: dict[str, Any] = {}
        for name, sd in (self.src.get("securityDefinitions") or {}).items():
            t = sd.get("type")
            entry = self._one_security_scheme(name, t, sd)
            if entry is not None:
                out[name] = entry
        return out

    def _one_security_scheme(self, name: str, t: str, sd: dict):
        """Return a valid OpenAPI 3 securityScheme, or None (dropped +
        recorded in x-s2o.lossy) when the source cannot yield one."""
        def drop(reason: str):
            self.lossy.append(
                f"securityDefinitions.{name}: {reason}; dropped from "
                "securitySchemes"
            )
            return None

        if t == "basic":
            entry = {"type": "http", "scheme": "basic"}
        elif t == "apiKey":
            if not sd.get("name") or sd.get("in") not in ("query", "header", "cookie"):
                return drop("apiKey missing a valid 'name'/'in'")
            entry = {"type": "apiKey", "name": sd["name"], "in": sd["in"]}
        elif t == "oauth2":
            raw_flow = sd.get("flow")
            mapped = self._OAUTH_FLOWS.get(raw_flow)
            if mapped is None:
                return drop(f"oauth2 flow '{raw_flow}' missing/unrecognized")
            flow_key, required_urls = mapped
            missing = [u for u in required_urls if not sd.get(u)]
            if missing:
                return drop(
                    f"oauth2 '{raw_flow}' flow missing required "
                    f"{', '.join(missing)}"
                )
            flow_obj: dict[str, Any] = {"scopes": sd.get("scopes") or {}}
            for u in required_urls:
                flow_obj[u] = sd[u]
            entry = {"type": "oauth2", "flows": {flow_key: flow_obj}}
        else:
            return drop(f"unknown security type '{t}'")

        if sd.get("description"):
            entry["description"] = sd["description"]
        return entry

    # -- top level -------------------------------------------------------------

    def _servers(self) -> list[dict]:
        host = self.src.get("host")
        base = self.src.get("basePath", "") or ""
        if base and not base.startswith("/"):
            self.assumptions.append(
                f"basePath '{base}' has no leading slash; normalized to '/{base}'"
            )
            base = "/" + base
        schemes = self.src.get("schemes")
        if not host:
            self.assumptions.append(
                "no 'host' declared; emitted relative server url "
                f"'{base or '/'}' (override endpoint at runtime)"
            )
            return [{"url": base or "/"}]
        if not schemes:
            schemes = ["https"]
            self.assumptions.append(
                "no 'schemes' declared; assumed https"
            )
        servers = []
        for s in schemes:
            url = f"{s}://{host}{base}"
            server: dict[str, Any] = {"url": url}
            # OpenAPI 3 requires every {variable} in a server URL to be
            # declared under `variables` (with a default)
            tvars = re.findall(r"{([^}]+)}", url)
            if tvars:
                server["variables"] = {v: {"default": ""} for v in tvars}
                self.assumptions.append(
                    f"server url '{url}' is templated; declared "
                    f"variables {tvars} with empty defaults (supply values "
                    "at runtime)"
                )
            servers.append(server)
        return servers

    def _gen_operation_id(self, method: str, path: str) -> str:
        raw = f"{method}_{path.strip('/') or 'root'}"
        raw = raw.replace("{", "").replace("}", "")
        return _ID_RE.sub("_", raw).strip("_")[:64]

    def _build_info(self) -> dict[str, Any]:
        # title and version are REQUIRED in OpenAPI 3; fill whichever is
        # missing (a present-but-partial info must still be completed)
        info = dict(self.src.get("info") or {})
        for field in ("title", "version"):
            val = info.get(field)
            if val is not None and not isinstance(val, str):
                # both MUST be strings in OpenAPI 3
                info[field] = str(val)
                self.assumptions.append(
                    f"info.{field} was not a string; coerced to "
                    f"'{info[field]}'"
                )
        if not info.get("title"):
            info["title"] = "API"
            self.assumptions.append("info.title missing; defaulted to 'API'")
        if not info.get("version"):
            info["version"] = "0.0.0"
            self.assumptions.append(
                "info.version missing; defaulted to '0.0.0'"
            )
        return info

    def _ensure_path_params(self, path: str, params: list, ctx: str) -> list:
        """Every {template} in the path MUST have an in:path parameter."""
        global_params = self.src.get("parameters") or {}
        present: set = set()
        for p in params:
            if not isinstance(p, dict):
                continue
            if p.get("in") == "path" and p.get("name"):
                present.add(p["name"])
            elif "$ref" in p:  # resolve to the referenced global parameter
                ref = global_params.get(p["$ref"].rsplit("/", 1)[-1])
                if isinstance(ref, dict) and ref.get("in") == "path":
                    present.add(ref.get("name"))
        for name in re.findall(r"{([^}]+)}", path):
            if name in present:
                continue
            params = params + [{
                "name": name, "in": "path", "required": True,
                "schema": {"type": "string"},
            }]
            present.add(name)
            self.assumptions.append(
                f"{ctx}: path template '{{{name}}}' had no parameter; "
                "injected a required string path parameter"
            )
        return params

    def convert(self) -> dict[str, Any]:
        src = self.src
        out: dict[str, Any] = {
            "openapi": "3.0.3",
            "info": self._build_info(),
            "servers": self._servers(),
            "paths": {},
        }
        for k in ("externalDocs", "security"):
            if k in src:
                out[k] = src[k]
        if "tags" in src:  # Tag Object requires a name
            tags = []
            for tag in src["tags"]:
                if isinstance(tag, dict) and tag.get("name"):
                    tags.append(tag)
                else:
                    self.lossy.append("a tag without a name was dropped")
            if tags:
                out["tags"] = tags
        # carry root-level vendor extensions
        for k, v in src.items():
            if k.startswith("x-"):
                out[k] = v

        used_ids: set[str] = set()
        for path, item in (src.get("paths") or {}).items():
            if path.startswith("x-"):  # Paths-object vendor extension
                out["paths"][path] = item
                continue
            # Paths Object keys must start with '/'
            out_path = path if path.startswith("/") else "/" + path
            if out_path != path:
                self.assumptions.append(
                    f"path '{path}' has no leading slash; normalized to "
                    f"'{out_path}'"
                )
            if not isinstance(item, dict):
                self.lossy.append(
                    f"path '{path}': non-object path item dropped"
                )
                continue
            if "$ref" in item:  # path item is a $ref (legal in OpenAPI 3)
                out["paths"][out_path] = {"$ref": self._fix_ref(item["$ref"])}
                continue
            new_item: dict[str, Any] = {}
            shared_raw = item.get("parameters", [])
            for k, v in item.items():
                if k.startswith("x-"):
                    new_item[k] = v
            for method in _METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                ctx = f"{method.upper()} {path}"
                new_op: dict[str, Any] = {}
                for k in ("summary", "description", "tags", "deprecated",
                          "externalDocs", "security"):
                    if k in op:
                        new_op[k] = op[k]
                for k, v in op.items():
                    if k.startswith("x-"):
                        new_op[k] = v

                op_id = op.get("operationId")
                if not op_id:
                    op_id = self._gen_operation_id(method, path)
                    self.assumptions.append(
                        f"{ctx}: no operationId; generated '{op_id}'"
                    )
                else:
                    normalized = _ID_RE.sub("_", op_id).strip("_")[:64]
                    if not normalized:  # e.g. operationId was "!!!"
                        normalized = self._gen_operation_id(method, path)
                    if normalized != op_id:
                        self.assumptions.append(
                            f"{ctx}: operationId '{op_id}' normalized to "
                            f"'{normalized}' (FastMCP tool-name convention)"
                        )
                        op_id = normalized
                # dedup after normalization/truncation, staying <= 64 chars
                deduped = _unique_id(op_id, used_ids)
                if deduped != op_id:
                    self.assumptions.append(
                        f"{ctx}: operationId '{op_id}' already used; "
                        f"renamed to '{deduped}'"
                    )
                op_id = deduped
                new_op["operationId"] = op_id

                raw_params = _merge_params(shared_raw, op.get("parameters", []))
                params, request_body = self._split_params(raw_params, op, ctx)
                params = self._ensure_path_params(path, params, ctx)
                if params:
                    new_op["parameters"] = params
                if request_body is not None:
                    new_op["requestBody"] = request_body

                responses = {
                    str(code): self._convert_response(resp, op, ctx, code)
                    for code, resp in (op.get("responses") or {}).items()
                }
                if not responses:  # Responses Object requires >= 1 response
                    responses = {"200": {"description": "OK"}}
                    self.assumptions.append(
                        f"{ctx}: no responses declared; added a generic "
                        "'200 OK' (required in OpenAPI 3)"
                    )
                new_op["responses"] = responses

                new_item[method] = new_op
            out["paths"][out_path] = new_item

        components: dict[str, Any] = {}
        if src.get("definitions"):
            components["schemas"] = {
                self._schema_key.get(name, name): self._fix_schema(schema)
                for name, schema in src["definitions"].items()
            }
        global_params = src.get("parameters") or {}
        conv_params = {}
        request_bodies = {}
        for name, p in global_params.items():
            if name in self._global_body_params:
                request_bodies[name] = self._body_to_request_body(p, {}, name)
            elif isinstance(p, dict) and p.get("in") == "formData":
                # inlined at each use site by _split_params; a standalone
                # formData parameter cannot exist in OpenAPI 3 components
                self.lossy.append(
                    f"parameters.{name}: global formData parameter has no "
                    "OpenAPI 3 component equivalent; inlined at use sites "
                    "and dropped from components"
                )
            else:
                conv_params[name] = self._convert_param(p, f"parameters.{name}")
        if conv_params:
            components["parameters"] = conv_params
        if request_bodies:
            components["requestBodies"] = request_bodies
        if src.get("responses"):
            components["responses"] = {
                name: self._convert_response(r, {}, f"responses.{name}")
                for name, r in src["responses"].items()
            }
        schemes = self._convert_security_schemes()
        if schemes:
            components["securitySchemes"] = schemes
        if components:
            out["components"] = components

        out["x-s2o"] = {
            "source": "swagger-2.0",
            "generator": f"spec2openapi/{_version}",
            "assumptions": self.assumptions,
            "lossy": self.lossy,
        }
        return out
