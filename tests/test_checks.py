"""check_fastmcp_ready: the static FastMCP contract as a library API (#87)."""
from __future__ import annotations

from spec2openapi import check_fastmcp_ready, convert_swagger


def _spec(paths):
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": paths}


def test_ready_spec_returns_empty():
    spec = _spec({"/a": {"get": {"operationId": "get_a",
                                 "responses": {"200": {"description": "ok"}}}}})
    assert check_fastmcp_ready(spec) == []


def test_missing_and_duplicate_operation_ids():
    spec = _spec({
        "/a": {"get": {"responses": {}}},
        "/b": {"get": {"operationId": "same", "responses": {}},
               "put": {"operationId": "same", "responses": {}}},
    })
    problems = check_fastmcp_ready(spec)
    assert any("missing operationId" in p for p in problems)
    assert any("duplicate operationIds" in p for p in problems)


def test_unsafe_and_overlong_tool_names():
    spec = _spec({
        "/a": {"get": {"operationId": "has space", "responses": {}}},
        "/b": {"get": {"operationId": "x" * 65, "responses": {}}},
    })
    problems = check_fastmcp_ready(spec)
    assert sum("not a safe MCP tool name" in p for p in problems) == 2


def test_normalization_collision_predicted():
    # distinct ids that FastMCP normalizes to the same tool name
    spec = _spec({
        "/a": {"get": {"operationId": "get.a", "responses": {}}},
        "/b": {"get": {"operationId": "get-a", "responses": {}}},
    })
    problems = check_fastmcp_ready(spec)
    assert any("collide after FastMCP normalization" in p
               and "get_a" in p for p in problems)


def test_soap_operation_without_wrapper_element():
    spec = _spec({"/op": {"post": {
        "operationId": "op", "x-soap": {"soapAction": ""},
        "responses": {}}}})
    assert any("x-soap.input.element missing" in p
               for p in check_fastmcp_ready(spec))


def test_malformed_documents_return_problems_not_crashes():
    for garbage in (None, [], {}, {"paths": None}, {"paths": {"/a": None}},
                    {"paths": {"/a": {"get": None}}},
                    {"paths": {"/a": {"get": {"operationId": 7,
                                              "responses": {}}}}}):
        problems = check_fastmcp_ready(garbage)
        assert isinstance(problems, list) and problems


def test_converted_swagger_output_is_ready():
    out = convert_swagger({
        "swagger": "2.0", "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"get": {"responses": {
            "200": {"description": "ok"}}}}},
    })
    assert check_fastmcp_ready(out) == []
