"""Advanced WSDL/XSD features: facets, docs, choice, inheritance,
simpleContent, defaults, headers, faults, rpc/literal, OpenAPI 3.1."""
from __future__ import annotations

from pathlib import Path

import pytest

from spec2openapi import BridgeOptions, convert_wsdl, from_openapi_spec

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def adv_spec():
    return convert_wsdl(str(FIXTURES / "advanced.wsdl"))


@pytest.fixture(scope="module")
def rpc_spec():
    return convert_wsdl(str(FIXTURES / "rpc.wsdl"))


def _input_schema(spec, op):
    return spec["paths"][f"/operations/{op}"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]


def test_facets_from_imported_xsd(adv_spec):
    props = _input_schema(adv_spec, "SubmitApplication")["properties"]
    discount = props["discount"]
    assert discount["minimum"] == 0.0
    assert discount["maximum"] == 100.0
    assert discount["multipleOf"] == 0.01
    assert "percentage between 0 and 100" in discount["description"]

    person = adv_spec["components"]["schemas"]["Person"]
    name_schema = person["properties"]["name"]
    assert name_schema["minLength"] == 1
    assert name_schema["maxLength"] == 80

    country = person["properties"]["country"]
    assert country["pattern"] == "[A-Z]{2}"
    assert country["minLength"] == 2 and country["maxLength"] == 2


def test_inheritance_flattened(adv_spec):
    person = adv_spec["components"]["schemas"]["Person"]
    # base type (BaseParty) fields are merged in, sequence order preserved
    assert list(person["properties"]) == ["id", "country", "name", "age"]
    assert person["required"] == ["id", "name"]


def test_documentation_extraction(adv_spec):
    person = adv_spec["components"]["schemas"]["Person"]
    assert person["properties"]["id"]["description"] == "Internal party identifier."
    props = _input_schema(adv_spec, "SubmitApplication")["properties"]
    assert props["applicant"]["description"] == (
        "The person submitting the application."
    )
    assert "application submissions" in adv_spec["info"]["description"].lower()


def test_choice_members_not_required(adv_spec):
    schema = _input_schema(adv_spec, "SubmitApplication")
    assert "email" not in schema.get("required", [])
    assert "phone" not in schema.get("required", [])
    assert schema["x-soap-choice"] == [
        {"members": ["email", "phone"], "required": True}
    ]
    assert "Exactly one of: email, phone." in schema["description"]


def test_simple_content_value_plus_attribute(adv_spec):
    money = adv_spec["components"]["schemas"]["Money"]
    assert money["x-soap-simple-content"] is True
    assert money["properties"]["value"]["type"] == "number"
    assert money["properties"]["value"]["xml"] == {"x-text": True}
    assert money["properties"]["currency"]["xml"] == {
        "name": "currency", "attribute": True,
    }
    assert set(money["required"]) == {"value", "currency"}


def test_default_value(adv_spec):
    props = _input_schema(adv_spec, "SubmitApplication")["properties"]
    assert props["mode"]["default"] == "standard"


def test_headers_and_faults_metadata(adv_spec):
    op = adv_spec["paths"]["/operations/SubmitApplication"]["post"]
    xsoap = op["x-soap"]
    assert xsoap["headers"] == [{
        "element": "AuthHeader",
        "namespace": "http://example.com/adv",
        "part": "header",
        "schema": "#/components/schemas/AuthHeader",
    }]
    assert xsoap["faults"][0]["name"] == "ValidationFault"
    assert xsoap["faults"][0]["schema"] == "#/components/schemas/ValidationError"
    assert "ValidationError" in adv_spec["components"]["schemas"]
    assert "AuthHeader" in adv_spec["components"]["schemas"]
    assert "ValidationFault" in op["responses"]["500"]["description"]
    assert "Requires SOAP header" in op["description"]


def test_rpc_literal_conversion(rpc_spec):
    op = rpc_spec["paths"]["/operations/Multiply"]["post"]
    assert op["x-soap"]["style"] == "rpc"
    assert op["x-soap"]["input"] == {
        "element": "Multiply", "namespace": "http://example.com/math",
    }
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert list(schema["properties"]) == ["a", "b"]
    # rpc parts are unqualified: no namespace in xml annotation
    assert schema["properties"]["a"]["xml"] == {"name": "a"}


def test_openapi_31_emission():
    spec = convert_wsdl(str(FIXTURES / "advanced.wsdl"), openapi_version="3.1")
    assert spec["openapi"] == "3.1.0"
    discount = _input_schema(spec, "SubmitApplication")["properties"]["discount"]
    assert discount["minimum"] == 0.0  # inclusive stays numeric


async def test_rpc_e2e_through_bridge(soap_server):
    opts = BridgeOptions(endpoint=f"{soap_server}/math", trust_env=False)
    spec = convert_wsdl(str(FIXTURES / "rpc.wsdl"))
    mcp = from_openapi_spec(spec, options=opts)
    from fastmcp import Client

    async with Client(mcp) as client:
        result = await client.call_tool("Multiply", {"a": 6, "b": 7})
        data = result.data if isinstance(result.data, dict) else None
        if data is None:
            import json

            data = json.loads(result.content[0].text)
        assert data == {"result": 42}


async def test_simple_content_and_choice_e2e(soap_server):
    opts = BridgeOptions(endpoint=f"{soap_server}/app", trust_env=False)
    spec = convert_wsdl(str(FIXTURES / "advanced.wsdl"))
    mcp = from_openapi_spec(spec, options=opts)
    from fastmcp import Client

    async with Client(mcp) as client:
        result = await client.call_tool(
            "SubmitApplication",
            {
                "applicant": {"id": "P1", "name": "Alice", "country": "KR"},
                "payment": {"value": 12.5, "currency": "USD"},
                "email": "a@example.com",
            },
        )
        data = result.data if isinstance(result.data, dict) else None
        if data is None:
            import json

            data = json.loads(result.content[0].text)
        # mock echoes the simpleContent text + currency attribute it received
        assert data["applicationId"] == "APP-Alice-USD-12.5"
        assert data["score"] == 87.5
