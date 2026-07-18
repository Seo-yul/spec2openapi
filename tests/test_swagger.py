"""Swagger 2.0 -> OpenAPI 3.x upgrade tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from spec2openapi import (ConversionError, convert_swagger, is_swagger2,
                          load_spec)
from spec2openapi.cli import main as cli_main

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def legacy():
    return load_spec(FIXTURES / "legacy-swagger.json")


@pytest.fixture(scope="module")
def upgraded(legacy):
    return convert_swagger(legacy)


def test_detection(legacy, upgraded):
    assert is_swagger2(legacy)
    assert not is_swagger2(upgraded)
    with pytest.raises(ConversionError):
        convert_swagger(upgraded)


def test_servers_from_host_basepath_schemes(upgraded):
    assert upgraded["openapi"] == "3.0.3"
    assert upgraded["servers"] == [
        {"url": "https://petstore.example.com/v2"},
        {"url": "http://petstore.example.com/v2"},
    ]


def test_query_params_wrapped_in_schema(upgraded):
    params = {p["name"]: p for p in upgraded["paths"]["/pets"]["get"]["parameters"]}
    limit = params["limit"]
    assert limit["schema"] == {
        "type": "integer", "format": "int32", "default": 20, "maximum": 100,
    }
    assert "type" not in limit  # moved into schema

    # collectionFormat csv -> form + explode false; multi -> explode true
    assert params["tags"]["style"] == "form"
    assert params["tags"]["explode"] is False
    assert params["statuses"]["explode"] is True


def test_body_param_becomes_request_body(upgraded):
    post = upgraded["paths"]["/pets"]["post"]
    assert "parameters" not in post
    rb = post["requestBody"]
    assert rb["$ref"] == "#/components/requestBodies/PetBody"
    body = upgraded["components"]["requestBodies"]["PetBody"]
    assert body["required"] is True
    assert body["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/NewPet"
    )


def test_formdata_with_file_becomes_multipart(upgraded):
    rb = upgraded["paths"]["/pets/{petId}/photo"]["post"]["requestBody"]
    content = rb["content"]["multipart/form-data"]["schema"]
    assert content["properties"]["photo"] == {
        "type": "string", "format": "binary",
    }
    assert content["required"] == ["photo"]


def test_missing_operation_id_generated(upgraded):
    get_pets = upgraded["paths"]["/pets"]["get"]
    assert get_pets["operationId"] == "get_pets"
    delete = upgraded["paths"]["/pets/{petId}"]["delete"]
    assert delete["operationId"] == "delete_pets_petId"
    assumptions = upgraded["x-s2o"]["assumptions"]
    assert any("no operationId" in a for a in assumptions)


def test_missing_produces_assumes_json(upgraded):
    resp = upgraded["paths"]["/pets"]["get"]["responses"]["200"]
    assert "application/json" in resp["content"]
    assert any("assumed application/json" in a
               for a in upgraded["x-s2o"]["assumptions"])
    # explicit produces respected
    resp2 = upgraded["paths"]["/pets/{petId}"]["get"]["responses"]["200"]
    assert set(resp2["content"]) == {"application/json", "application/xml"}


def test_refs_rewritten_and_globals_moved(upgraded):
    comps = upgraded["components"]
    assert "Pet" in comps["schemas"]
    assert comps["parameters"]["petId"]["schema"]["type"] == "integer"
    assert comps["responses"]["NotFound"]["content"]["application/json"][
        "schema"]["$ref"] == "#/components/schemas/Error"
    r404 = upgraded["paths"]["/pets"]["get"]["responses"]["404"]
    assert r404["$ref"] == "#/components/responses/NotFound"
    shared = upgraded["paths"]["/pets/{petId}"]["get"]["parameters"]
    assert {"$ref": "#/components/parameters/petId"} in shared


def test_schema_fixups(upgraded):
    schemas = upgraded["components"]["schemas"]
    assert schemas["NewPet"]["properties"]["tag"]["nullable"] is True
    assert schemas["Animal"]["discriminator"] == {"propertyName": "petType"}
    assert schemas["Pet"]["allOf"][0]["$ref"] == "#/components/schemas/NewPet"


def test_security_schemes(upgraded):
    ss = upgraded["components"]["securitySchemes"]
    assert ss["basic_auth"] == {"type": "http", "scheme": "basic"}
    assert ss["api_key"] == {"type": "apiKey", "name": "X-API-Key", "in": "header"}
    flows = ss["petstore_auth"]["flows"]
    assert "authorizationCode" in flows
    assert flows["authorizationCode"]["tokenUrl"] == "https://auth.example.com/token"
    assert flows["authorizationCode"]["scopes"]["read:pets"] == "read pets"


def test_response_headers_and_examples(upgraded):
    resp = upgraded["paths"]["/pets"]["get"]["responses"]["200"]
    assert resp["headers"]["X-Rate-Limit"]["schema"]["type"] == "integer"
    created = upgraded["paths"]["/pets"]["post"]["responses"]["201"]
    assert created["content"]["application/json"]["example"] == {
        "id": 1, "name": "Bella",
    }


def test_openapi_31_variant(legacy):
    spec31 = convert_swagger(legacy, openapi_version="3.1")
    assert spec31["openapi"] == "3.1.0"
    tag = spec31["components"]["schemas"]["NewPet"]["properties"]["tag"]
    assert tag["type"] == ["string", "null"]  # nullable -> type union


def test_spec_validator_passes(upgraded):
    from openapi_spec_validator import validate as osv_validate

    osv_validate(upgraded)


async def test_fastmcp_roundtrip(upgraded):
    op_ids = {
        op["operationId"]
        for item in upgraded["paths"].values()
        for method, op in item.items()
        if isinstance(op, dict) and "operationId" in op
    }
    mcp = FastMCP.from_openapi(openapi_spec=upgraded, name="s2o")
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == op_ids
    # query params surface as tool args
    assert "limit" in tools["get_pets"].inputSchema["properties"]
    # body schema properties surface too
    assert "name" in tools["addPet"].inputSchema["properties"]


def test_upgrade_cli(tmp_path, capsys):
    out = tmp_path / "upgraded.yaml"
    rc = cli_main(["upgrade", str(FIXTURES / "legacy-swagger.json"),
                   "-o", str(out)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "assumption" in err
    spec = load_spec(out)
    assert spec["openapi"] == "3.0.3"

    # validate auto-detects swagger input and upgrades in memory
    rc = cli_main(["validate", str(FIXTURES / "legacy-swagger.json")])
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "FastMCP round-trip: OK" in captured.out
