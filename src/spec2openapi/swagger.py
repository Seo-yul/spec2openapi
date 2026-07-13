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
from typing import Any

from . import __version__ as _version
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


def is_swagger2(spec: dict[str, Any]) -> bool:
    return str(spec.get("swagger", "")).startswith("2")


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
    spec: dict[str, Any], *, openapi_version: str = "3.0"
) -> dict[str, Any]:
    """Upgrade a Swagger 2.0 dict to OpenAPI 3.0 (or 3.1)."""
    if not is_swagger2(spec):
        raise ValueError("not a Swagger 2.0 document (missing swagger: '2.0')")
    up = _Upgrader(spec)
    out = up.convert()
    if openapi_version.startswith("3.1"):
        out = to_openapi_31(out)
    return out


class _Upgrader:
    def __init__(self, src: dict[str, Any]):
        self.src = src
        self.assumptions: list[str] = []
        self.lossy: list[str] = []
        # global body-parameters become requestBodies, not parameters
        self._global_body_params: set[str] = {
            name
            for name, p in (src.get("parameters") or {}).items()
            if isinstance(p, dict) and p.get("in") == "body"
        }

    # -- helpers -----------------------------------------------------------

    def _fix_ref(self, ref: str) -> str:
        if ref.startswith("#/definitions/"):
            return ref.replace("#/definitions/", "#/components/schemas/", 1)
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
            elif k in _DATA_KEYWORDS:
                out[k] = v  # data value: pass through verbatim
            else:
                out[k] = self._fix_schema(v)
        if out.get("type") == "file":
            out["type"] = "string"
            out["format"] = "binary"
        return out

    def _media_types(self, kind: str, op: dict, ctx: str) -> list[str]:
        types = op.get(kind) or self.src.get(kind) or []
        if not types:
            self.assumptions.append(
                f"{ctx}: no '{kind}' declared; assumed application/json"
            )
            return ["application/json"]
        return list(types)

    # -- parameters --------------------------------------------------------

    def _convert_param(self, p: dict, ctx: str) -> dict:
        """query/path/header (non-body, non-formData) parameter."""
        out = {
            k: v
            for k, v in p.items()
            if k in ("name", "in", "description", "required", "allowEmptyValue")
            or k.startswith("x-")
        }
        schema = {k: self._fix_schema(v) for k, v in p.items()
                  if k in _SCHEMA_FIELDS}
        if schema.get("type") == "file":
            schema = {"type": "string", "format": "binary"}
        out["schema"] = schema or {"type": "string"}

        cf = p.get("collectionFormat")
        if cf:
            loc = p.get("in")
            if cf == "csv":
                out["style"] = "form" if loc in ("query", "formData") else "simple"
                out["explode"] = False
            elif cf == "multi":
                out["style"] = "form"
                out["explode"] = True
            elif cf == "ssv":
                out["style"] = "spaceDelimited"
                out["explode"] = False
            elif cf == "pipes":
                out["style"] = "pipeDelimited"
                out["explode"] = False
            else:  # tsv and friends have no OpenAPI 3 equivalent
                out["x-collectionFormat"] = cf
                self.lossy.append(
                    f"{ctx}: collectionFormat '{cf}' has no OpenAPI 3 "
                    "equivalent; preserved as x-collectionFormat"
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
            schema = {k: self._fix_schema(v) for k, v in p.items()
                      if k in _SCHEMA_FIELDS}
            if p.get("type") == "file":
                schema = {"type": "string", "format": "binary"}
            if p.get("description"):
                schema["description"] = p["description"]
            props[p["name"]] = schema or {"type": "string"}
            if p.get("required"):
                required.append(p["name"])
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
                continue
            if "$ref" in p:
                ref = self._fix_ref(p["$ref"])
                if "/requestBodies/" in ref:
                    body = {"$ref": ref}
                else:
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

    def _convert_response(self, resp: dict, op: dict, ctx: str) -> dict:
        if "$ref" in resp:
            return {"$ref": self._fix_ref(resp["$ref"])}
        out: dict[str, Any] = {
            "description": resp.get("description", "")
        }
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

    def _convert_security_schemes(self) -> dict:
        out: dict[str, Any] = {}
        flow_names = {
            "implicit": "implicit",
            "password": "password",
            "application": "clientCredentials",
            "accessCode": "authorizationCode",
        }
        for name, sd in (self.src.get("securityDefinitions") or {}).items():
            t = sd.get("type")
            if t == "basic":
                out[name] = {"type": "http", "scheme": "basic"}
                if sd.get("description"):
                    out[name]["description"] = sd["description"]
            elif t == "apiKey":
                out[name] = {k: sd[k] for k in ("type", "name", "in")
                             if k in sd}
                if sd.get("description"):
                    out[name]["description"] = sd["description"]
            elif t == "oauth2":
                raw_flow = sd.get("flow")
                flow = flow_names.get(raw_flow)
                if flow is None:
                    flow = "implicit"
                    self.lossy.append(
                        f"securityDefinitions.{name}: oauth2 flow "
                        f"'{raw_flow}' missing/unrecognized; assumed "
                        "'implicit'"
                    )
                flow_obj: dict[str, Any] = {
                    "scopes": sd.get("scopes", {}),
                }
                if "authorizationUrl" in sd:
                    flow_obj["authorizationUrl"] = sd["authorizationUrl"]
                if "tokenUrl" in sd:
                    flow_obj["tokenUrl"] = sd["tokenUrl"]
                entry: dict[str, Any] = {
                    "type": "oauth2", "flows": {flow: flow_obj},
                }
                if sd.get("description"):
                    entry["description"] = sd["description"]
                out[name] = entry
            else:
                out[name] = dict(sd)
                self.lossy.append(
                    f"securityDefinitions.{name}: unknown type '{t}' copied as-is"
                )
        return out

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
        return [{"url": f"{s}://{host}{base}"} for s in schemes]

    def _gen_operation_id(self, method: str, path: str) -> str:
        raw = f"{method}_{path.strip('/') or 'root'}"
        raw = raw.replace("{", "").replace("}", "")
        return _ID_RE.sub("_", raw).strip("_")[:64]

    def convert(self) -> dict[str, Any]:
        src = self.src
        if "info" not in src:
            self.assumptions.append(
                "no 'info' object; used default title 'API' version '0.0.0'"
            )
        out: dict[str, Any] = {
            "openapi": "3.0.3",
            "info": dict(src.get("info", {"title": "API", "version": "0.0.0"})),
            "servers": self._servers(),
            "paths": {},
        }
        for k in ("tags", "externalDocs", "security"):
            if k in src:
                out[k] = src[k]
        # carry root-level vendor extensions
        for k, v in src.items():
            if k.startswith("x-"):
                out[k] = v

        used_ids: set[str] = set()
        for path, item in (src.get("paths") or {}).items():
            if path.startswith("x-"):  # Paths-object vendor extension
                out["paths"][path] = item
                continue
            if not isinstance(item, dict):
                continue
            if "$ref" in item:  # path item is a $ref (legal in OpenAPI 3)
                out["paths"][path] = {"$ref": self._fix_ref(item["$ref"])}
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
                op_id = _unique_id(op_id, used_ids)
                new_op["operationId"] = op_id

                raw_params = _merge_params(shared_raw, op.get("parameters", []))
                params, request_body = self._split_params(raw_params, op, ctx)
                if params:
                    new_op["parameters"] = params
                if request_body is not None:
                    new_op["requestBody"] = request_body

                new_op["responses"] = {
                    str(code): self._convert_response(resp, op, ctx)
                    for code, resp in (op.get("responses") or {}).items()
                } or {"200": {"description": "OK"}}

                new_item[method] = new_op
            out["paths"][path] = new_item

        components: dict[str, Any] = {}
        if src.get("definitions"):
            components["schemas"] = {
                name: self._fix_schema(schema)
                for name, schema in src["definitions"].items()
            }
        global_params = src.get("parameters") or {}
        conv_params = {}
        request_bodies = {}
        for name, p in global_params.items():
            if name in self._global_body_params:
                request_bodies[name] = self._body_to_request_body(p, {}, name)
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
