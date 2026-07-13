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
