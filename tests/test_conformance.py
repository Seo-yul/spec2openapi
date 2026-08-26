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


# -- string-valued consumes/produces (#55) ------------------------------------

@pytest.mark.parametrize("kind,where", [("consumes", "op"), ("produces", "op"),
                                        ("consumes", "root"), ("produces", "root")])
def test_string_media_types_wrapped_not_split(kind, where):
    op = {"operationId": "a",
          "parameters": [{"name": "b", "in": "body", "schema": {"type": "object"}}],
          "responses": {"200": {"description": "ok", "schema": {"type": "string"}}}}
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"post": op}}}
    (op if where == "op" else src)[kind] = "application/json"  # bare string
    out = _valid(src)
    post = out["paths"]["/a"]["post"]
    content = (post["requestBody"]["content"] if kind == "consumes"
               else post["responses"]["200"]["content"])
    assert list(content.keys()) == ["application/json"]  # not ['a','p','l',…]
    assert any("was a string" in a for a in out["x-s2o"]["assumptions"])


# -- operation-level empty consumes/produces clears the global (#141 C) --------

@pytest.mark.parametrize("kind", ["consumes", "produces"])
def test_operation_level_empty_media_types_does_not_inherit_global(kind):
    # an operation-level `consumes: []`/`produces: []` is a deliberate
    # override, not "unspecified" — it must not fall back to the global
    # declaration (previously `op.get(kind) or self.src.get(kind)`
    # treated the empty list as falsy and inherited the global anyway).
    op = {"operationId": "a", kind: [],
          "parameters": [{"name": "b", "in": "body",
                          "schema": {"type": "object"}}],
          "responses": {"200": {"description": "ok",
                                "schema": {"type": "string"}}}}
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           kind: ["application/xml"], "paths": {"/a": {"post": op}}}
    out = _valid(src)
    post = out["paths"]["/a"]["post"]
    content = (post["requestBody"]["content"] if kind == "consumes"
               else post["responses"]["200"]["content"])
    # falls back to the documented application/json default, NOT the
    # global 'application/xml'
    assert list(content.keys()) == ["application/json"]


# -- more GIGO hardening (#57) -------------------------------------------------

def test_boolean_required_hoisted_to_parent():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": {},
           "definitions": {"T": {"type": "object", "properties": {
               "a": {"type": "string", "required": True},
               "b": {"type": "string", "required": False}}}}}
    out = _valid(src)
    t = out["components"]["schemas"]["T"]
    assert t["required"] == ["a"]
    assert "required" not in t["properties"]["a"]
    assert "required" not in t["properties"]["b"]


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_type_null_literal_becomes_nullable(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": {},
           "definitions": {"T": {"type": "null"}}}
    out = _valid(src, version)
    assert out["components"]["schemas"]["T"].get("type") != "null"


def test_tuple_items_collapsed():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": {},
           "definitions": {
               "One": {"type": "array", "items": [{"type": "string"}]},
               "Two": {"type": "array",
                       "items": [{"type": "string"}, {"type": "integer"}]}}}
    out = _valid(src)
    assert out["components"]["schemas"]["One"]["items"] == {"type": "string"}
    assert "anyOf" in out["components"]["schemas"]["Two"]["items"]


def test_non_string_info_coerced():
    src = {"swagger": "2.0", "info": {"title": "t", "version": 2}, "paths": {}}
    out = _valid(src)
    assert out["info"]["version"] == "2"


def test_null_values_stripped_but_data_nulls_kept():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {
               "operationId": "a", "summary": None,
               "responses": {"200": {"description": "ok",
                                     "schema": {"type": "string", "format": None}}}}}},
           "definitions": {"T": {"type": "string", "nullable": True,
                                 "enum": ["a", None], "default": None}}}
    out = _valid(src)
    assert "summary" not in out["paths"]["/a"]["get"]          # structural null gone
    t = out["components"]["schemas"]["T"]
    assert t["enum"] == ["a", None] and "default" in t          # data nulls kept
    assert any("null value" in a for a in out["x-s2o"]["assumptions"])


# -- global formData parameters (#59) ------------------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_global_formdata_param_inlined(version):
    """A $ref to a global formData parameter is merged into the form
    requestBody; the global entry is dropped from components (found on a
    real-world corpus spec)."""
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"post": {
            "operationId": "a",
            "parameters": [{"$ref": "#/parameters/cb"},
                           {"name": "q", "in": "query", "type": "string"}],
            "responses": {"200": {"description": "ok"}},
        }}},
        "parameters": {"cb": {"name": "callback", "in": "formData",
                              "type": "string"}},
    }
    out = _valid(src, version)
    post = out["paths"]["/a"]["post"]
    schema = post["requestBody"]["content"][
        "application/x-www-form-urlencoded"]["schema"]
    assert "callback" in schema["properties"]           # inlined
    names = [p.get("name") for p in post.get("parameters", [])]
    assert names == ["q"]                               # no leftover $ref
    comp_params = out.get("components", {}).get("parameters", {})
    assert "cb" not in comp_params                      # dropped from components
    assert any("formData parameter" in m for m in out["x-s2o"]["lossy"])


# -- status-phrase descriptions (#61) ------------------------------------------

def test_missing_description_filled_with_status_phrase():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {"operationId": "a", "responses": {
               "200": {"schema": {"type": "string"}},
               "404": {"schema": {"type": "string"}},
               "599": {"schema": {"type": "string"}},   # unknown code
               "default": {"schema": {"type": "string"}},
           }}}}}
    out = _valid(src)
    resps = out["paths"]["/a"]["get"]["responses"]
    assert resps["200"]["description"] == "OK"
    assert resps["404"]["description"] == "Not Found"
    assert resps["599"]["description"] == ""            # unknown -> empty
    assert resps["default"]["description"] == "Default response"
    assert any("missing 'description'" in a for a in out["x-s2o"]["assumptions"])


def test_existing_description_untouched():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {"operationId": "a", "responses": {
               "200": {"description": "custom"}}}}}}
    out = _valid(src)
    assert out["paths"]["/a"]["get"]["responses"]["200"]["description"] == "custom"


# -- strict mode + recording completeness (#63) --------------------------------

def test_allow_empty_value_drop_recorded():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a/{id}": {"get": {
               "operationId": "a",
               "parameters": [{"name": "id", "in": "path", "required": True,
                               "type": "string", "allowEmptyValue": True}],
               "responses": {"200": {"description": "ok"}}}}}}
    out = _valid(src)
    assert any("allowEmptyValue" in m for m in out["x-s2o"]["lossy"])


def test_operationid_dedup_recorded():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {
               "/a": {"get": {"operationId": "same",
                              "responses": {"200": {"description": "ok"}}}},
               "/b": {"get": {"operationId": "same",
                              "responses": {"200": {"description": "ok"}}}}}}
    out = _valid(src)
    assert any("renamed to 'same_2'" in a for a in out["x-s2o"]["assumptions"])


# -- response header schema fixups + collectionFormat recording (#141 C) ------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_response_header_multitype_array_collapses(version):
    # a response header's schema fields were fixed one field at a time,
    # so a multi-type array on 'type' never reached _fix_schema's
    # collapse logic and stayed an invalid JSON-Schema type array.
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {
               "operationId": "a",
               "responses": {"200": {"description": "ok",
                   "headers": {"X-Count": {"type": ["integer", "null"]}}}}}}}}
    out = _valid(src, version)
    schema = out["paths"]["/a"]["get"]["responses"]["200"][
        "headers"]["X-Count"]["schema"]
    if version == "3.0":
        assert schema["type"] == "integer" and schema["nullable"] is True
    else:
        assert set(schema["type"]) == {"integer", "null"}


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_response_header_collection_format_recorded(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {
               "operationId": "a",
               "responses": {"200": {"description": "ok",
                   "headers": {"X-Tags": {
                       "type": "array", "items": {"type": "string"},
                       "collectionFormat": "csv"}}}}}}}}
    out = _valid(src, version)
    schema = out["paths"]["/a"]["get"]["responses"]["200"][
        "headers"]["X-Tags"]["schema"]
    assert "collectionFormat" not in schema
    assert schema["x-collectionFormat"] == "csv"
    assert any("collectionFormat" in m for m in out["x-s2o"]["lossy"])


# -- path collision after leading-slash normalization is recorded (#141 C) -----

def test_path_collision_after_normalization_recorded():
    # 'pets' normalizes to '/pets', silently overwriting the path item
    # already declared at '/pets' — must be recorded, not silent.
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {
               "/pets": {"get": {"operationId": "a",
                                 "responses": {"200": {"description": "ok"}}}},
               "pets": {"post": {"operationId": "b",
                                 "responses": {"200": {"description": "ok"}}}},
           }}
    out = _valid(src)
    assert any("collide" in m for m in out["x-s2o"]["lossy"])
    assert len(out["paths"]) == 1  # one survives at the normalized key
    assert "get" in out["paths"]["/pets"] or "post" in out["paths"]["/pets"]


def test_strict_raises_with_records_listed():
    from spec2openapi import ConversionError
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {  # no operationId, no host -> assumptions
               "responses": {"200": {"description": "ok"}}}}}}
    with pytest.raises(ConversionError) as exc:
        convert_swagger(src, strict=True)
    msg = str(exc.value)
    assert "strict mode" in msg
    assert "operationId" in msg  # the actual records are listed


def test_strict_passes_clean_spec():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "host": "api.example.com", "schemes": ["https"],
           "paths": {"/a": {"get": {
               "operationId": "a", "produces": ["application/json"],
               "responses": {"200": {"description": "ok"}}}}}}
    out = convert_swagger(src, strict=True)
    assert out["x-s2o"]["assumptions"] == [] and out["x-s2o"]["lossy"] == []


# -- templated server variables (#65) ------------------------------------------

def test_templated_host_declares_server_variables():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "host": "{region}.example.com", "basePath": "/v{apiVersion}",
           "paths": {}}
    out = _valid(src)
    srv = out["servers"][0]
    assert set(srv["variables"]) == {"region", "apiVersion"}
    assert all("default" in v for v in srv["variables"].values())
    assert any("templated" in a for a in out["x-s2o"]["assumptions"])


def test_plain_host_has_no_variables():
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "host": "api.example.com", "paths": {}}
    out = _valid(src)
    assert "variables" not in out["servers"][0]


# -- $ref siblings preserved via allOf (#67) -----------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_ref_siblings_wrapped_in_allof(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "A": {"type": "object"},
               "B": {"type": "object", "properties": {
                   "x": {"$ref": "#/definitions/A", "description": "keep me"},
                   "y": {"$ref": "#/definitions/A"}}}}}
    out = _valid(src, version)
    props = out["components"]["schemas"]["B"]["properties"]
    assert props["x"]["allOf"] == [{"$ref": "#/components/schemas/A"}]
    assert props["x"]["description"] == "keep me"       # sibling stays alive
    assert props["y"] == {"$ref": "#/components/schemas/A"}  # bare untouched
    assert any("allOf" in a for a in out["x-s2o"]["assumptions"])


# -- $ref + sibling allOf must merge, not clobber (#141 A1) --------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_ref_with_sibling_allof_merged_not_overwritten(version):
    # a $ref alongside a sibling *allOf* (not just plain keys like
    # 'description') must not have the $ref silently dropped by the
    # dict-spread `{"allOf": [ref], **out}` when `out` already has its
    # own 'allOf' key.
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "A": {"type": "object"},
               "B": {"type": "object", "properties": {
                   "x": {"$ref": "#/definitions/A",
                         "allOf": [{"description": "extra"}]}}}}}
    out = _valid(src, version)
    allof = out["components"]["schemas"]["B"]["properties"]["x"]["allOf"]
    refs = [m["$ref"] for m in allof if isinstance(m, dict) and "$ref" in m]
    assert refs == ["#/components/schemas/A"]
    assert {"description": "extra"} in allof


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_deep_ref_with_sibling_allof_merged(version):
    # same bug, deep-$ref hoisting path (swagger.py's other copy of the
    # allOf-wrap logic).
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {
               "operationId": "a",
               "responses": {"200": {"description": "ok", "schema": {
                   "type": "object", "properties": {
                       "word": {"type": "string"}}}}}}}},
           "definitions": {"Uses": {"type": "object", "properties": {
               "w": {"$ref": "#/paths/~1a/get/responses/200/schema"
                             "/properties/word",
                     "allOf": [{"description": "extra"}]}}}}}
    out = _valid(src, version)
    allof = out["components"]["schemas"]["Uses"]["properties"]["w"]["allOf"]
    assert any(isinstance(m, dict) and "$ref" in m for m in allof)
    assert {"description": "extra"} in allof


# -- multi-type collapse must not skip file/items fixups (#141 A2) -------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_multitype_array_collapse_still_gets_items(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {"T": {"type": ["array", "null"]}}}
    out = _valid(src, version)
    t = out["components"]["schemas"]["T"]
    assert t.get("items") == {}
    if version == "3.0":
        assert t["type"] == "array" and t["nullable"] is True
    else:
        assert set(t["type"]) == {"array", "null"}


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_multitype_file_collapse_still_gets_binary_fixup(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {"T": {"type": ["file"]}}}
    out = _valid(src, version)
    t = out["components"]["schemas"]["T"]
    assert t["type"] == "string"
    assert t["format"] == "binary"


# -- 'default' response key must not be treated as opaque data (#141 A3) -------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_default_response_key_not_treated_as_opaque_data(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {
               "operationId": "a",
               "responses": {
                   "200": {"description": "ok", "schema": {
                       "type": "object", "properties": None}},
                   "default": {"description": "err", "schema": {
                       "type": "object", "properties": None}},
               }}}}}
    out = _valid(src, version)
    resps = out["paths"]["/a"]["get"]["responses"]
    s200 = resps["200"]["content"]["application/json"]["schema"]
    sdef = resps["default"]["content"]["application/json"]["schema"]
    assert "properties" not in s200
    assert "properties" not in sdef
    assert sdef == s200


# -- deep documents must not RecursionError (#141 A4) ---------------------------

def _deep_swagger_definition(depth):
    node = {"type": "string"}
    for _ in range(depth):
        node = {"type": "object", "properties": {"a": node}}
    return node


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_deeply_nested_definition_does_not_crash(version):
    # _strip_nulls/_fix_schema/to_openapi_31.walk used to recurse via raw
    # Python call-stack frames; a ~1000-deep schema raised a bare
    # RecursionError instead of the documented ConversionError contract.
    from spec2openapi import ConversionError

    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {"Deep": _deep_swagger_definition(1000)}}
    try:
        out = convert_swagger(src, openapi_version=version)
    except ConversionError:
        return  # a clean, documented refusal is an acceptable outcome
    validate(out)  # or a full, valid conversion is also acceptable


def test_moderately_nested_definition_converts_successfully():
    # a realistic depth must never be falsely rejected by a depth cap
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {"Deep": _deep_swagger_definition(50)}}
    out = _valid(src)
    node = out["components"]["schemas"]["Deep"]
    for _ in range(50):
        node = node["properties"]["a"]
    assert node["type"] == "string"


# -- component-key sanitization beyond schemas (#72) ---------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_all_component_namespaces_sanitized(version):
    """securityDefinitions / global parameters / responses names with
    invalid characters get valid component keys, with every reference
    (security requirements, $refs) rewritten to match."""
    src = {
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "securityDefinitions": {"Basic Auth": {"type": "basic"}},
        "security": [{"Basic Auth": []}],
        "parameters": {
            "filter[code]": {"name": "code", "in": "query", "type": "string"},
            "the body!": {"name": "b", "in": "body",
                          "schema": {"type": "object"}},
        },
        "responses": {"Not Found!": {"description": "nf"}},
        "paths": {"/a": {
            "get": {
                "operationId": "a",
                "security": [{"Basic Auth": []}],
                "parameters": [{"$ref": "#/parameters/filter[code]"}],
                "responses": {"404": {"$ref": "#/responses/Not Found!"},
                              "200": {"description": "ok"}},
            },
            "post": {
                "operationId": "b",
                "parameters": [{"$ref": "#/parameters/the body!"}],
                "responses": {"200": {"description": "ok"}},
            },
        }},
    }
    out = _valid(src, version)
    c = out["components"]
    key_re = __import__("re").compile(r"^[a-zA-Z0-9._-]+$")
    for ns in ("securitySchemes", "parameters", "requestBodies", "responses"):
        assert all(key_re.match(k) for k in c.get(ns, {})), ns
    # references follow the sanitized keys
    assert out["security"] == [{"Basic_Auth": []}]
    get_op = out["paths"]["/a"]["get"]
    assert get_op["security"] == [{"Basic_Auth": []}]
    assert get_op["parameters"][0]["$ref"].endswith("/filter_code")
    assert get_op["responses"]["404"]["$ref"].endswith("/Not_Found")
    assert out["paths"]["/a"]["post"]["requestBody"]["$ref"].endswith("/the_body")
    assert any("renamed to component key" in a
               for a in out["x-s2o"]["assumptions"])


# -- percent-encoded $ref tokens (#74) -----------------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_percent_encoded_ref_follows_sanitized_key(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "Ref (of Bundle)": {"type": "object"},
               "B": {"type": "object", "properties": {
                   "x": {"$ref": "#/definitions/Ref%20(of%20Bundle)"}}}}}
    out = _valid(src, version)
    key = next(k for k in out["components"]["schemas"] if k != "B")
    assert out["components"]["schemas"]["B"]["properties"]["x"]["$ref"] == (
        f"#/components/schemas/{key}")


# -- default coercion (#73) ----------------------------------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_defaults_coerced_or_dropped(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "A": {"type": "integer", "default": "1"},
               "B": {"type": "string", "default": 123456789},
               "C": {"type": "boolean", "default": "false"},
               "D": {"type": "string", "pattern": "^[a-z]+$", "default": ""},
               "E": {"type": "string", "default": "valid"},
               "F": {"type": "string", "nullable": True, "default": None},
           }}
    out = _valid(src, version)
    s = out["components"]["schemas"]
    assert s["A"]["default"] == 1                      # str -> int
    assert s["B"]["default"] == "123456789"            # int -> str
    assert s["C"]["default"] is False                  # 'false' -> bool
    assert "default" not in s["D"]                     # pattern-violating: dropped
    assert s["E"]["default"] == "valid"                # valid: untouched
    assert "default" in s["F"] and s["F"]["default"] is None  # null kept
    assert any("coerced" in a for a in out["x-s2o"]["assumptions"])
    assert any("dropped" in m for m in out["x-s2o"]["lossy"])


# -- collectionFormat inside an Items Object (#76) -----------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_items_collectionformat_preserved_as_extension(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {"parameters": [
               {"in": "query", "name": "order_by", "type": "array",
                "items": {"type": "string", "collectionFormat": "csv"}}],
               "responses": {"200": {"description": "ok"}}}}}}
    out = _valid(src, version)
    items = out["paths"]["/a"]["get"]["parameters"][0]["schema"]["items"]
    assert "collectionFormat" not in items
    assert items["x-collectionFormat"] == "csv"
    assert any("collectionFormat" in m for m in out["x-s2o"]["lossy"])


# -- deep local $refs into arbitrary document locations (#75) ------------------

def _deref(out, node):
    """Follow a #/components/schemas/... $ref one hop if present."""
    if isinstance(node, dict) and set(node) == {"$ref"}:
        return out["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_deep_paths_ref_hoisted(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {
               "/word/{id}": {"get": {
                   "parameters": [{"in": "path", "name": "id",
                                   "required": True, "type": "string"}],
                   "responses": {"200": {"description": "ok", "schema": {
                       "type": "array", "items": {"type": "object",
                           "properties": {"word": {"type": "string",
                                                   "x-nullable": True}}}}}}}},
               "/lexeme/{id}": {"get": {
                   "parameters": [{"in": "path", "name": "id",
                                   "required": True, "type": "string"}],
                   "responses": {"200": {"description": "ok", "schema": {
                       "type": "object", "properties": {
                           "lexeme": {"$ref": "#/paths/~1word~1%7Bid%7D/get"
                                              "/responses/200/schema/items"
                                              "/properties/word"},
                           "annotated": {"$ref": "#/paths/~1word~1%7Bid%7D"
                                                 "/get/responses/200/schema"
                                                 "/items/properties/word",
                                         "description": "kept"}}}}}}}}}
    out = _valid(src, version)
    sch = out["paths"]["/lexeme/{id}"]["get"]["responses"]["200"][
        "content"]["application/json"]["schema"]
    def is_string(schema):  # 3.0: nullable; 3.1: type ['string', 'null']
        t = schema["type"]
        return t == "string" or (isinstance(t, list) and "string" in t)

    lexeme = _deref(out, sch["properties"]["lexeme"])
    assert is_string(lexeme)
    annotated = sch["properties"]["annotated"]  # siblings -> allOf wrap
    assert is_string(_deref(out, annotated["allOf"][0]))
    assert annotated["description"] == "kept"
    # both use sites share ONE hoisted component (no duplication)
    assert (sch["properties"]["lexeme"]["$ref"]
            == annotated["allOf"][0]["$ref"])
    assert any("hoisted" in a for a in out["x-s2o"]["assumptions"])


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_deep_definitions_ref_hoisted(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "Foo": {"type": "object",
                       "properties": {"bar": {"type": "integer"}}},
               "Uses": {"type": "object", "properties": {
                   "b": {"$ref": "#/definitions/Foo/properties/bar"}}}}}
    out = _valid(src, version)
    b = _deref(out, out["components"]["schemas"]["Uses"]["properties"]["b"])
    assert b == {"type": "integer"}


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_cyclic_and_unresolvable_deep_refs_are_neutralized(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "A": {"type": "object", "properties": {
                   "self": {"$ref": "#/definitions/A/properties/self"}}},
               "B": {"properties": {
                   "gone": {"$ref": "#/paths/~1nope/get"}}}}}
    out = _valid(src, version)
    s = out["components"]["schemas"]
    # the pure alias cycle is broken to the empty schema
    assert _deref(out, s["A"]["properties"]["self"]) == {}
    assert s["B"]["properties"]["gone"] == {}
    assert any("cyclic" in m for m in out["x-s2o"]["lossy"])
    assert any("unresolvable" in m for m in out["x-s2o"]["lossy"])


# -- dangling simple $refs and keyword-named properties (#96) ------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_dangling_simple_ref_becomes_empty_schema(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "A": {"type": "object", "properties": {
                   "x": {"$ref": "#/definitions/Missing"},
                   "y": {"$ref": "#/parameters/NopeParam"}}}}}
    out = _valid(src, version)
    props = out["components"]["schemas"]["A"]["properties"]
    assert props["x"] == {} and props["y"] == {}
    assert sum("unresolvable local $ref" in m
               for m in out["x-s2o"]["lossy"]) == 2


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_properties_named_like_keywords_are_schemas(version):
    # a property may be literally named default/enum/discriminator/… —
    # the keyword handling must not fire on the properties map itself
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "Ok": {"type": "string"},
               "T": {"type": "object", "properties": {
                   "default": {"$ref": "#/definitions/Ok"},
                   "enum": {"type": "string", "x-nullable": True},
                   "discriminator": {"type": "string"},
                   "collectionFormat": {"type": "integer"},
                   "example": {"$ref": "#/definitions/Ok"}}}}}
    out = _valid(src, version)
    t = out["components"]["schemas"]["T"]["properties"]
    assert t["default"] == {"$ref": "#/components/schemas/Ok"}
    assert t["discriminator"] == {"type": "string"}
    assert t["collectionFormat"] == {"type": "integer"}
    assert "nullable" in t["enum"] or t["enum"].get("type") == ["string", "null"]


# -- opaque property-map keys must survive to_openapi_31 (#141 A6) -------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_opaque_property_named_nullable_survives_31(version):
    # a property literally named "nullable"/"enum" must not be deleted or
    # left invalid by to_openapi_31's own keyword handling — the
    # properties map's keys are opaque names, not schema keywords, the
    # same rule swagger.py's own _fix_schema already applies.
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "T": {"type": "object", "properties": {
                   "nullable": {"type": "string"},
                   "enum": {"type": "integer"},
                   "exclusiveMinimum": {"type": "boolean"}}}}}
    out = _valid(src, version)
    props = out["components"]["schemas"]["T"]["properties"]
    assert set(props) == {"nullable", "enum", "exclusiveMinimum"}
    assert props["nullable"]["type"] == "string"
    assert props["enum"]["type"] == "integer"
    assert props["exclusiveMinimum"]["type"] == "boolean"


def test_opaque_property_own_nullable_still_converts_in_31():
    # a property literally named "enum" whose OWN schema has "nullable"
    # must still get that nullable correctly re-encoded for 3.1 — the
    # data-keyword passthrough must not also suppress recursion into a
    # legitimately-schema-shaped value sitting at a colliding key.
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "T": {"type": "object", "properties": {
                   "enum": {"type": "string", "x-nullable": True}}}}}
    out = _valid(src, "3.1")
    prop = out["components"]["schemas"]["T"]["properties"]["enum"]
    assert "nullable" not in prop
    assert set(prop["type"]) == {"string", "null"}


# -- nullable on a non-string-type schema must survive to_openapi_31 (#141 A5) -

def _accepts_null(schema) -> bool:
    """Best-effort structural check that a JSON-Schema-2020-12 fragment
    accepts a null instance (no live $ref-resolving validator needed for
    this unit test: the fixtures below never rely on resolving a $ref
    branch to prove nullability — the null-accepting branch is always
    self-contained)."""
    if not isinstance(schema, dict):
        return False
    t = schema.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    if isinstance(schema.get("enum"), list) and None in schema["enum"]:
        return True
    if any(_accepts_null(b) for b in schema.get("anyOf") or ()):
        return True
    if any(_accepts_null(b) for b in schema.get("oneOf") or ()):
        return True
    allof = schema.get("allOf")
    if allof:
        return all(_accepts_null(b) for b in allof)
    return False


def test_nullable_ref_sibling_survives_31():
    # x-nullable next to a $ref (wrapped in allOf by swagger.py, no
    # sibling 'type' to fold nullable into) must not vanish in 3.1.
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "A": {"type": "object"},
               "B": {"type": "object", "properties": {
                   "x": {"x-nullable": True, "$ref": "#/definitions/A"}}}}}
    out = _valid(src, "3.1")
    schema = out["components"]["schemas"]["B"]["properties"]["x"]
    assert "nullable" not in schema
    assert _accepts_null(schema)


def test_nullable_enum_only_survives_31():
    # nullable on an enum-only (typeless) schema must not vanish either.
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "T": {"nullable": True, "enum": ["a", "b"]}}}
    out = _valid(src, "3.1")
    schema = out["components"]["schemas"]["T"]
    assert "nullable" not in schema
    assert _accepts_null(schema)


# -- parameter-position $refs and duplicates (#97, #101) -----------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_duplicate_component_param_refs_merged(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "parameters": {"sub": {"name": "sub", "in": "query",
                                  "type": "string"}},
           "paths": {"/a": {
               "parameters": [{"$ref": "#/parameters/sub"}],
               "get": {"parameters": [{"$ref": "#/parameters/sub"}],
                       "responses": {"200": {"description": "ok"}}}}}}
    out = _valid(src, version)
    plist = out["paths"]["/a"]["get"]["parameters"]
    assert plist == [{"$ref": "#/components/parameters/sub"}]
    assert any("merged" in a for a in out["x-s2o"]["assumptions"])


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_deep_parameter_ref_inlined_and_deduped(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {
               "/tgt": {"post": {"parameters": [
                   {"name": "api-version", "in": "query", "required": True,
                    "type": "string"}],
                   "responses": {"200": {"description": "ok"}}}},
               "/use/{id}": {"get": {"parameters": [
                   {"$ref": "#/paths/~1tgt/post/parameters/0"},
                   {"name": "api-version", "in": "query", "required": True,
                    "type": "string"},
                   {"name": "id", "in": "path", "required": True,
                    "type": "string"}],
                   "responses": {"200": {"description": "ok"}}}}}}
    out = _valid(src, version)
    names = [(p.get("name"), p.get("in"))
             for p in out["paths"]["/use/{id}"]["get"]["parameters"]]
    assert names == [("api-version", "query"), ("id", "path")]
    assert any("target inlined" in a for a in out["x-s2o"]["assumptions"])


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_unresolvable_parameter_ref_dropped(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {"parameters": [
               {"$ref": "#/paths/~1nope/get/parameters/9"},
               {"name": "q", "in": "query", "type": "string"}],
               "responses": {"200": {"description": "ok"}}}}}}
    out = _valid(src, version)
    assert [p["name"] for p in out["paths"]["/a"]["get"]["parameters"]] == ["q"]
    assert any("unresolvable parameter $ref" in m
               for m in out["x-s2o"]["lossy"])


def test_hoisted_output_stays_linear():
    # 30 use sites of one deep ref must share one hoisted component,
    # not inline 30 copies (#101)
    import json as _json
    big = {"type": "object", "properties": {
        f"f{i}": {"type": "string"} for i in range(50)}}
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "Big": {"type": "object", "properties": {"inner": big}},
               **{f"U{i}": {"properties": {
                   "u": {"$ref": "#/definitions/Big/properties/inner"}}}
                  for i in range(30)}}}
    out = _valid(src, "3.0")
    assert len(_json.dumps(out)) < 3 * len(_json.dumps(src))
    hoisted = [k for k in out["components"]["schemas"]
               if k.startswith("definitions_Big")]
    assert len(hoisted) == 1


# -- uncompilable pattern preserved as x-pattern (#98) -------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_uncompilable_pattern_moved_to_extension(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "A": {"type": "string", "pattern": "^[\\p{Han}]*$"},
               "B": {"type": "string", "pattern": "^[A-Za-z0-9][\\w-\\.]*$"},
               "C": {"type": "string", "pattern": "^[a-z]+$"}}}
    out = _valid(src, version)
    s = out["components"]["schemas"]
    assert "pattern" not in s["A"] and s["A"]["x-pattern"] == "^[\\p{Han}]*$"
    assert "pattern" not in s["B"] and "x-pattern" in s["B"]
    assert s["C"]["pattern"] == "^[a-z]+$"  # a valid pattern is untouched
    assert sum("preserved as x-pattern" in m
               for m in out["x-s2o"]["lossy"]) == 2


# -- x-example / x-oneOf / x-anyOf promotion (#95) -----------------------------

@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_parameter_x_example_promoted(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {"/a/{id}": {"get": {"parameters": [
               {"name": "id", "in": "path", "required": True,
                "type": "string", "x-example": "CMUC"}],
               "responses": {"200": {"description": "ok"}}}}}}
    out = _valid(src, version)
    p = out["paths"]["/a/{id}"]["get"]["parameters"][0]
    assert p["example"] == "CMUC" and "x-example" not in p
    assert any("x-example promoted" in a for a in out["x-s2o"]["assumptions"])


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_x_oneof_and_x_anyof_promoted(version):
    src = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
           "paths": {}, "definitions": {
               "common": {"type": "object"},
               "T": {"type": "object",
                     "x-oneOf": [{"$ref": "#/definitions/common"},
                                 {"type": "string"}]},
               "V": {"type": "object", "oneOf": [{"type": "integer"}],
                     "x-oneOf": [{"type": "string"}]}}}
    out = _valid(src, version)
    s = out["components"]["schemas"]
    # promoted, members run through the schema fixups ($ref rewritten)
    assert s["T"]["oneOf"][0] == {"$ref": "#/components/schemas/common"}
    assert "x-oneOf" not in s["T"]
    # a native keyword already present wins; the extension is kept as-is
    assert s["V"]["oneOf"] == [{"type": "integer"}] and "x-oneOf" in s["V"]
    assert any("promoted to native oneOf" in a
               for a in out["x-s2o"]["assumptions"])


def test_default_response_subtree_is_converted_to_31():
    """responses 의 "default" 는 스키마의 default 값이 아니라 응답 이름이다.

    data 키워드로 보고 통째로 복사하면 그 안의 nullable 이 변환되지 않은
    채 남아, JSON Schema 2020-12 가 모르는 키워드를 든 3.1 문서가 나간다.
    """
    from spec2openapi.openapi import to_openapi_31

    def _body():
        return {"content": {"application/json":
                            {"schema": {"type": "string", "nullable": True}}}}

    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/x": {"get": {"operationId": "gx", "responses": {
                "200": dict(description="ok", **_body()),
                "default": dict(description="err", **_body())}}}}}
    out = to_openapi_31(spec)
    responses = out["paths"]["/x"]["get"]["responses"]
    for name in ("200", "default"):
        schema = responses[name]["content"]["application/json"]["schema"]
        assert "nullable" not in schema, name
        assert schema["type"] == ["string", "null"], name


def test_media_type_examples_are_not_walked_as_schemas():
    """Example Object 의 value 는 사용자 데이터다.

    스키마로 착각해 walk 하면 사용자가 예시로 실어둔 nullable 이 type
    배열로 바뀌어, 예시가 원본과 달라진다.
    """
    from spec2openapi.openapi import to_openapi_31

    payload = {"nullable": True, "type": "string"}
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/x": {"get": {"operationId": "gx", "responses": {
                "200": {"description": "ok", "content": {"application/json": {
                    "schema": {"type": "object"},
                    "examples": {"sample": {"value": payload}}}}}}}}}}
    out = to_openapi_31(spec)
    got = (out["paths"]["/x"]["get"]["responses"]["200"]
           ["content"]["application/json"]["examples"]["sample"]["value"])
    assert got == payload
