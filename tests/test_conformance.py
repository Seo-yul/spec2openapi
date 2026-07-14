"""OpenAPI 3.0/3.1 conformance regression tests for the Swagger upgrader.

Each test converts an adversarial Swagger 2.0 input and asserts the output
passes openapi-spec-validator (i.e. the converter never emits a spec that
violates the OpenAPI schema).
"""
from __future__ import annotations

import pytest

from spec2openapi import convert_swagger

validate = pytest.importorskip("openapi_spec_validator").validate

BASE = {"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": {}}


def _valid(src, version="3.0"):
    out = convert_swagger(src, openapi_version=version)
    validate(out)
    return out


# -- H1/H2/H3 security schemes ----------------------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
@pytest.mark.parametrize("defs", [
    {"x": {"type": "weird", "name": "a"}},                       # unknown type
    {"k": {"type": "apiKey"}},                                   # apiKey no name/in
    {"k": {"type": "apiKey", "name": "X-Key"}},                  # apiKey no in
    {"o": {"type": "oauth2", "flow": "implicit", "scopes": {}}},  # oauth2 no url
    {"o": {"type": "oauth2", "scopes": {}}},                     # oauth2 no flow
    {"o": {"type": "oauth2", "flow": "accessCode",
           "authorizationUrl": "https://x/a", "scopes": {}}},     # missing tokenUrl
])
def test_invalid_security_dropped_not_emitted(defs, version):
    out = _valid({**BASE, "securityDefinitions": defs}, version)
    schemes = out.get("components", {}).get("securitySchemes", {})
    assert schemes == {}  # the invalid scheme was dropped
    assert out["x-s2o"]["lossy"]  # and recorded


# -- H4 partial info ---------------------------------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
@pytest.mark.parametrize("info", [
    {"title": "t"},        # no version
    {"version": "1"},      # no title
    {},                    # empty
])
def test_partial_info_completed(info, version):
    out = _valid({"swagger": "2.0", "info": info, "paths": {}}, version)
    assert out["info"]["title"]
    assert out["info"]["version"]


def test_missing_info_completed():
    out = _valid({"swagger": "2.0", "paths": {}})
    assert out["info"] == {"title": "API", "version": "0.0.0"}


# -- H7 unresolved path template --------------------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_unresolved_path_template_injected(version):
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a/{id}": {"get": {
            "operationId": "a", "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src, version)
    params = out["paths"]["/a/{id}"]["get"]["parameters"]
    injected = [p for p in params if p.get("name") == "id"]
    assert injected and injected[0]["in"] == "path"
    assert injected[0]["required"] is True
    assert any("path template" in a for a in out["x-s2o"]["assumptions"])


def test_partly_declared_path_templates():
    """One template declared, one missing -> only the missing one injected."""
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a/{x}/{y}": {"get": {
            "operationId": "a",
            "parameters": [{"name": "x", "in": "path", "required": True,
                            "type": "string"}],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src, "3.1")
    names = [p["name"] for p in out["paths"]["/a/{x}/{y}"]["get"]["parameters"]
             if p.get("in") == "path"]
    assert sorted(names) == ["x", "y"]


# -- component key charset ---------------------------------------------------

def test_definition_names_sanitized_and_refs_rewritten():
    import re
    key_re = re.compile(r"^[a-zA-Z0-9._-]+$")
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"post": {
            "operationId": "a",
            "parameters": [{"name": "b", "in": "body",
                            "schema": {"$ref": "#/definitions/Foo Bar"}}],
            "responses": {"200": {"description": "ok"}},
        }}},
        "definitions": {
            "Foo Bar": {"type": "object",
                        "properties": {"c": {"$ref": "#/definitions/Foo/Bar"}}},
            "Foo/Bar": {"type": "object"},
        },
    }
    out = _valid(src)
    keys = list(out["components"]["schemas"])
    assert all(key_re.match(k) for k in keys)          # all keys valid
    assert len(keys) == len(set(keys)) == 2            # collision deduped
    # every $ref points at a real, sanitized key
    body_ref = out["paths"]["/a"]["post"]["requestBody"]["content"][
        "application/json"]["schema"]["$ref"]
    assert body_ref.rsplit("/", 1)[-1] in keys
    nested_ref = out["components"]["schemas"][
        body_ref.rsplit("/", 1)[-1]]["properties"]["c"]["$ref"]
    assert nested_ref.rsplit("/", 1)[-1] in keys


# -- path leading slash / tag name / array items -----------------------------

def test_path_gets_leading_slash():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"noslash": {"get": {
               "operationId": "a", "responses": {"200": {"description": "ok"}},
           }}}}
    out = _valid(src)
    assert "/noslash" in out["paths"]
    assert "noslash" not in out["paths"]


def test_tag_without_name_dropped():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "tags": [{"description": "x"}, {"name": "good"}]}
    out = _valid(src)
    assert out["tags"] == [{"name": "good"}]
    assert out["x-s2o"]["lossy"]


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_array_without_items_gets_items(version):
    # in a definition
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {"T": {"type": "array"}}}
    out = _valid(src, version)
    assert out["components"]["schemas"]["T"].get("items") == {}
    # in a parameter
    src2 = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
            "paths": {"/a": {"get": {
                "operationId": "a",
                "parameters": [{"name": "q", "in": "query", "type": "array"}],
                "responses": {"200": {"description": "ok"}},
            }}}}
    out2 = _valid(src2, version)
    assert out2["paths"]["/a"]["get"]["parameters"][0]["schema"]["items"] == {}


# -- explicit required on every parameter ------------------------------------

def test_required_always_explicit():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a/{id}": {"get": {
            "operationId": "a",
            "parameters": [
                {"name": "id", "in": "path", "type": "string"},        # no required
                {"name": "q1", "in": "query", "type": "string", "required": True},
                {"name": "q2", "in": "query", "type": "string"},        # no required
                {"name": "h", "in": "header", "type": "string"},        # no required
            ],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src)
    got = {p["name"]: p for p in out["paths"]["/a/{id}"]["get"]["parameters"]}
    # every parameter carries an explicit boolean 'required'
    assert all(isinstance(p["required"], bool) for p in got.values())
    assert got["id"]["required"] is True    # path always true
    assert got["q1"]["required"] is True     # source value kept
    assert got["q2"]["required"] is False    # explicit default
    assert got["h"]["required"] is False


# -- H5 allowEmptyValue location --------------------------------------------

def test_allow_empty_value_dropped_off_query():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a/{id}": {"get": {
            "operationId": "a",
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "type": "string", "allowEmptyValue": True}],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src, "3.1")  # 3.1 rejects allowEmptyValue on path
    p = out["paths"]["/a/{id}"]["get"]["parameters"][0]
    assert "allowEmptyValue" not in p


def test_allow_empty_value_kept_on_query():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {
            "operationId": "a",
            "parameters": [{"name": "q", "in": "query", "type": "string",
                            "allowEmptyValue": True}],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src)
    assert out["paths"]["/a"]["get"]["parameters"][0]["allowEmptyValue"] is True


# -- H6 collectionFormat location -------------------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
@pytest.mark.parametrize("loc", ["path", "header"])
def test_collection_format_multi_on_path_header(loc, version):
    p = {"name": "v", "in": loc, "type": "array",
         "items": {"type": "string"}, "collectionFormat": "multi"}
    if loc == "path":
        p["required"] = True
    path = "/a/{v}" if loc == "path" else "/a"
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {path: {"get": {
            "operationId": "a", "parameters": [p],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src, version)
    param = out["paths"][path]["get"]["parameters"][0]
    assert param.get("style") == "simple"  # not the invalid 'form'


def test_collection_format_query_still_form():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {
            "operationId": "a",
            "parameters": [{"name": "q", "in": "query", "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "csv"}],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src)
    assert out["paths"]["/a"]["get"]["parameters"][0]["style"] == "form"


# -- H8 formData without a name ---------------------------------------------

def test_formdata_without_name_does_not_crash():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"post": {
            "operationId": "a",
            "parameters": [
                {"in": "formData", "type": "string"},        # no name
                {"name": "ok", "in": "formData", "type": "string"},
            ],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src)  # must not raise
    schema = out["paths"]["/a"]["post"]["requestBody"]["content"][
        "application/x-www-form-urlencoded"]["schema"]
    assert set(schema["properties"]) == {"ok"}
    assert out["x-s2o"]["lossy"]


# -- safe defaults are recorded (never invent silently) ----------------------

def test_synthesized_responses_recorded():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {"operationId": "a"}}}}  # no responses
    out = _valid(src)
    assert out["paths"]["/a"]["get"]["responses"]  # a 200 was synthesized
    assert any("no responses" in a for a in out["x-s2o"]["assumptions"])


def test_missing_response_description_recorded():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {
               "operationId": "a",
               "responses": {"200": {"schema": {"type": "string"}}},  # no description
           }}}}
    out = _valid(src)
    assert any("description" in a for a in out["x-s2o"]["assumptions"])


def test_non_object_param_dropped_recorded():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {
               "operationId": "a", "parameters": ["garbage"],
               "responses": {"200": {"description": "ok"}},
           }}}}
    out = _valid(src)
    assert any("non-object parameter" in m for m in out["x-s2o"]["lossy"])


# -- M1-M4 GIGO hardening ----------------------------------------------------

def test_non_boolean_required_coerced():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {
            "operationId": "a",
            "parameters": [{"name": "q", "in": "query", "type": "string",
                            "required": "yes"}],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src)
    assert out["paths"]["/a"]["get"]["parameters"][0]["required"] is True


def test_nameless_non_path_param_dropped():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {
            "operationId": "a",
            "parameters": [{"in": "query", "type": "string"},
                           {"name": "ok", "in": "query", "type": "string"}],
            "responses": {"200": {"description": "ok"}},
        }}},
    }
    out = _valid(src)
    names = [p.get("name") for p in out["paths"]["/a"]["get"]["parameters"]]
    assert names == ["ok"]
    assert out["x-s2o"]["lossy"]


def test_type_array_collapsed_in_30_reexpanded_in_31():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {}, "definitions": {"T": {"type": ["string", "null"]}},
    }
    out30 = _valid(src, "3.0")
    assert out30["components"]["schemas"]["T"] == {
        "type": "string", "nullable": True}
    out31 = _valid(src, "3.1")
    assert out31["components"]["schemas"]["T"]["type"] == ["string", "null"]


def test_discriminator_without_property_name_dropped():
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {},
        "definitions": {"T": {"type": "object",
                              "discriminator": {"mapping": {"a": "#/x"}}}},
    }
    out = _valid(src)
    assert "discriminator" not in out["components"]["schemas"]["T"]
    assert out["x-s2o"]["lossy"]


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_valid_security_preserved(version):
    defs = {
        "b": {"type": "basic"},
        "k": {"type": "apiKey", "name": "X", "in": "header"},
        "o": {"type": "oauth2", "flow": "implicit",
              "authorizationUrl": "https://x/a", "scopes": {"r": "read"}},
    }
    out = _valid({**BASE, "securityDefinitions": defs}, version)
    schemes = out["components"]["securitySchemes"]
    assert set(schemes) == {"b", "k", "o"}
    assert schemes["b"] == {"type": "http", "scheme": "basic"}
