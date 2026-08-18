"""WSDL -> OpenAPI conversion tests."""
from __future__ import annotations

from spec2openapi import convert_wsdl


def test_calculator_paths_and_types(calculator_wsdl):
    spec = convert_wsdl(calculator_wsdl)
    assert spec["openapi"] == "3.0.3"
    assert set(spec["paths"]) == {"/operations/Add", "/operations/Divide"}

    add = spec["paths"]["/operations/Add"]["post"]
    assert add["operationId"] == "Add"
    assert "Adds two integers" in add["description"]

    xsoap = add["x-soap"]
    assert xsoap["soapAction"] == "http://example.com/calculator/Add"
    assert xsoap["soapVersion"] == "1.1"
    assert xsoap["endpoint"] == "http://localhost:18080/calc"
    assert xsoap["input"] == {
        "element": "Add",
        "namespace": "http://example.com/calculator",
    }

    schema = add["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["a"]["type"] == "integer"
    assert schema["properties"]["b"]["type"] == "integer"
    assert schema["required"] == ["a", "b"]

    out = add["responses"]["200"]["content"]["application/json"]["schema"]
    assert out["properties"]["result"]["type"] == "integer"

    div_out = spec["paths"]["/operations/Divide"]["post"]["responses"]["200"][
        "content"]["application/json"]["schema"]
    assert div_out["properties"]["result"]["type"] == "number"

    assert "SoapFault" in spec["components"]["schemas"]
    assert spec["servers"][0]["url"] == "http://localhost:18080"


def test_orders_complex_types(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)
    create = spec["paths"]["/operations/CreateOrder"]["post"]
    schema = create["requestBody"]["content"]["application/json"]["schema"]
    props = schema["properties"]

    # nested complex type via $ref
    assert props["customer"]["allOf"][0]["$ref"] == "#/components/schemas/Customer"
    # unbounded element -> array with minItems
    assert props["items"]["type"] == "array"
    assert props["items"]["minItems"] == 1
    assert props["items"]["items"]["$ref"] == "#/components/schemas/Item"
    # optional element not required
    assert "note" not in schema["required"]
    assert list(props) == ["customer", "items", "note"]  # XSD sequence order kept

    comps = spec["components"]["schemas"]
    item = comps["Item"]
    assert item["properties"]["gift"]["xml"] == {"name": "gift", "attribute": True}
    assert item["properties"]["price"]["type"] == "number"
    customer = comps["Customer"]
    assert customer["required"] == ["name"]

    out = create["responses"]["200"]["content"]["application/json"]["schema"]
    assert out["properties"]["status"]["enum"] == ["NEW", "SHIPPED", "CANCELLED"]
    assert out["properties"]["createdAt"]["format"] == "date-time"


def test_dual_port_dedup_and_soap_version_choice():
    from pathlib import Path

    wsdl = str(Path(__file__).parent / "fixtures" / "dualport.wsdl")

    spec = convert_wsdl(wsdl)
    assert list(spec["paths"]) == ["/operations/Echo"]  # deduped across ports
    assert spec["paths"]["/operations/Echo"]["post"]["x-soap"]["soapVersion"] == "1.1"

    spec12 = convert_wsdl(wsdl, prefer_soap12=True)
    xsoap = spec12["paths"]["/operations/Echo"]["post"]["x-soap"]
    assert xsoap["soapVersion"] == "1.2"
    assert xsoap["endpoint"].endswith("/echo12")


def test_xml_annotations_carry_namespace(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)
    schema = spec["paths"]["/operations/CreateOrder"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]
    assert schema["properties"]["items"]["xml"]["namespace"] == (
        "http://example.com/orders"
    )


def test_soap_fault_schema_independent_per_conversion(calculator_wsdl):
    """#142: build_spec() aliased the module-level SOAP_FAULT_SCHEMA dict
    into conv.components by reference, and the shallow dict(conv.components)
    kept that identity — every 3.0 spec shared one process-global mutable
    SoapFault schema. Each conversion (and the template itself) must own
    an independent object; mutating one must not leak into another
    conversion or into the global template."""
    from spec2openapi.openapi import SOAP_FAULT_SCHEMA

    spec1 = convert_wsdl(calculator_wsdl)
    spec2 = convert_wsdl(calculator_wsdl)
    fault1 = spec1["components"]["schemas"]["SoapFault"]
    fault2 = spec2["components"]["schemas"]["SoapFault"]

    assert fault1 is not SOAP_FAULT_SCHEMA
    assert fault1 is not fault2

    # mutate spec1's copy, including a nested dict (guards against a
    # shallow copy that would still alias the "properties" sub-dict)
    fault1["properties"]["faultcode"]["type"] = "integer"
    fault1["description"] = "mutated"

    assert SOAP_FAULT_SCHEMA["properties"]["faultcode"]["type"] == "string"
    assert SOAP_FAULT_SCHEMA["description"] != "mutated"
    assert fault2["properties"]["faultcode"]["type"] == "string"
    assert fault2["description"] != "mutated"


def test_forbid_external_allows_local_imports():
    """forbid_external blocks remote fetches only; local imports still work."""
    from pathlib import Path

    wsdl = str(Path(__file__).parent / "fixtures" / "advanced.wsdl")
    spec = convert_wsdl(wsdl, forbid_external=True)
    assert spec["paths"]
    # facets scraped from the locally xsd:import-ed schema survive
    schemas = spec["components"]["schemas"]
    assert any("pattern" in str(s) for s in schemas.values())
