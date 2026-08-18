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


# -- numeric XSD enum / large bounds must not lose type or precision (#141 A7) -

_ENUM_WSDL = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
  xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:tns="urn:enumtest" targetNamespace="urn:enumtest">
  <types>
    <xsd:schema targetNamespace="urn:enumtest" elementFormDefault="qualified">
      <xsd:simpleType name="Level">
        <xsd:restriction base="xsd:int">
          <xsd:enumeration value="1"/>
          <xsd:enumeration value="2"/>
        </xsd:restriction>
      </xsd:simpleType>
      <xsd:simpleType name="BigBound">
        <xsd:restriction base="xsd:long">
          <xsd:minInclusive value="0"/>
          <xsd:maxInclusive value="9223372036854775807"/>
        </xsd:restriction>
      </xsd:simpleType>
      <xsd:simpleType name="Ratio">
        <xsd:restriction base="xsd:decimal">
          <xsd:enumeration value="0.5"/>
          <xsd:enumeration value="1.5"/>
        </xsd:restriction>
      </xsd:simpleType>
      <xsd:simpleType name="Code">
        <xsd:restriction base="xsd:string">
          <xsd:enumeration value="A"/>
          <xsd:enumeration value="B"/>
        </xsd:restriction>
      </xsd:simpleType>
      <xsd:element name="EnumOp">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="level" type="tns:Level"/>
            <xsd:element name="bound" type="tns:BigBound"/>
            <xsd:element name="ratio" type="tns:Ratio"/>
            <xsd:element name="code" type="tns:Code"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
      <xsd:element name="EnumOpResponse">
        <xsd:complexType>
          <xsd:sequence><xsd:element name="ok" type="xsd:boolean"/></xsd:sequence>
        </xsd:complexType>
      </xsd:element>
    </xsd:schema>
  </types>
  <message name="EnumOpIn"><part name="p" element="tns:EnumOp"/></message>
  <message name="EnumOpOut"><part name="p" element="tns:EnumOpResponse"/></message>
  <portType name="pt"><operation name="EnumOp">
    <input message="tns:EnumOpIn"/><output message="tns:EnumOpOut"/>
  </operation></portType>
  <binding name="b" type="tns:pt">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="EnumOp"><soap:operation soapAction="urn:EnumOp"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>
  <service name="s"><port name="p" binding="tns:b">
    <soap:address location="http://x/y"/></port></service>
</definitions>
"""


@pytest.fixture(scope="module")
def enum_spec():
    return convert_wsdl(content=_ENUM_WSDL)


def test_int_based_enum_coerced_to_integer(enum_spec):
    level = _input_schema(enum_spec, "EnumOp")["properties"]["level"]
    assert level["type"] == "integer"
    assert level["enum"] == [1, 2]  # not the unsatisfiable ["1", "2"]


def test_long_based_bounds_keep_int64_precision(enum_spec):
    bound = _input_schema(enum_spec, "EnumOp")["properties"]["bound"]
    assert bound["type"] == "integer"
    assert bound["minimum"] == 0
    assert bound["maximum"] == 9223372036854775807
    assert isinstance(bound["maximum"], int)  # not a float (precision loss)


def test_decimal_based_enum_coerced_to_number(enum_spec):
    ratio = _input_schema(enum_spec, "EnumOp")["properties"]["ratio"]
    assert ratio["type"] == "number"
    assert ratio["enum"] == [0.5, 1.5]


def test_string_based_enum_stays_string(enum_spec):
    code = _input_schema(enum_spec, "EnumOp")["properties"]["code"]
    assert code["type"] == "string"
    assert code["enum"] == ["A", "B"]


def test_openapi_31_enum_spec_still_validates():
    from openapi_spec_validator import validate as osv_validate

    spec = convert_wsdl(content=_ENUM_WSDL, openapi_version="3.1")
    osv_validate(spec)


# -- nillable complex/$ref elements must not lose the annotation (#141 C) -----

_NIL_WSDL = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
  xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:tns="urn:nilltest" targetNamespace="urn:nilltest">
  <types>
    <xsd:schema targetNamespace="urn:nilltest" elementFormDefault="qualified">
      <xsd:complexType name="Address">
        <xsd:sequence>
          <xsd:element name="city" type="xsd:string"/>
        </xsd:sequence>
      </xsd:complexType>
      <xsd:element name="NilOp">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="home" type="tns:Address" nillable="true"/>
            <xsd:element name="note" type="xsd:string" nillable="true"/>
            <xsd:element name="stops" type="tns:Address" nillable="true"
                         minOccurs="0" maxOccurs="unbounded"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
      <xsd:element name="NilOpResponse">
        <xsd:complexType>
          <xsd:sequence><xsd:element name="ok" type="xsd:boolean"/></xsd:sequence>
        </xsd:complexType>
      </xsd:element>
    </xsd:schema>
  </types>
  <message name="NilOpIn"><part name="p" element="tns:NilOp"/></message>
  <message name="NilOpOut"><part name="p" element="tns:NilOpResponse"/></message>
  <portType name="pt"><operation name="NilOp">
    <input message="tns:NilOpIn"/><output message="tns:NilOpOut"/>
  </operation></portType>
  <binding name="b" type="tns:pt">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="NilOp"><soap:operation soapAction="urn:NilOp"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>
  <service name="s"><port name="p" binding="tns:b">
    <soap:address location="http://x/y"/></port></service>
</definitions>
"""


def test_nillable_complex_ref_element_preserved():
    # nillable was silently dropped whenever the element's type was
    # complex (a $ref): OpenAPI 3.0 ignores 'nullable' as a bare $ref
    # sibling, so it must be wrapped in allOf like other $ref-sibling
    # cases in this codebase, not just discarded.
    spec = convert_wsdl(content=_NIL_WSDL)
    schema = _input_schema(spec, "NilOp")

    home = schema["properties"]["home"]
    assert home["nullable"] is True
    assert home["allOf"][0]["$ref"].endswith("/Address")

    note = schema["properties"]["note"]  # simple type: baseline, unaffected
    assert note["nullable"] is True

    stops_items = schema["properties"]["stops"]["items"]
    assert stops_items["nullable"] is True
    assert stops_items["allOf"][0]["$ref"].endswith("/Address")


def test_nillable_complex_ref_valid_in_31():
    from openapi_spec_validator import validate as osv_validate

    spec = convert_wsdl(content=_NIL_WSDL, openapi_version="3.1")
    osv_validate(spec)


# -- complexType with only xsd:any must not be misjudged as simpleContent -----
# (#141 C: zeep names an xsd:any particle "_value_1", same as the
# synthetic name it gives xsd:simpleContent's implicit text value)

_ANY_ONLY_WSDL = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
  xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:tns="urn:anytest" targetNamespace="urn:anytest">
  <types>
    <xsd:schema targetNamespace="urn:anytest" elementFormDefault="qualified">
      <xsd:complexType name="AnyOnly">
        <xsd:sequence>
          <xsd:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
        </xsd:sequence>
      </xsd:complexType>
      <xsd:element name="AnyOp">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="payload" type="tns:AnyOnly"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
      <xsd:element name="AnyOpResponse">
        <xsd:complexType>
          <xsd:sequence><xsd:element name="ok" type="xsd:boolean"/></xsd:sequence>
        </xsd:complexType>
      </xsd:element>
    </xsd:schema>
  </types>
  <message name="AnyOpIn"><part name="p" element="tns:AnyOp"/></message>
  <message name="AnyOpOut"><part name="p" element="tns:AnyOpResponse"/></message>
  <portType name="pt"><operation name="AnyOp">
    <input message="tns:AnyOpIn"/><output message="tns:AnyOpOut"/>
  </operation></portType>
  <binding name="b" type="tns:pt">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="AnyOp"><soap:operation soapAction="urn:AnyOp"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>
  <service name="s"><port name="p" binding="tns:b">
    <soap:address location="http://x/y"/></port></service>
</definitions>
"""


def test_any_only_complextype_not_misjudged_as_simple_content():
    spec = convert_wsdl(content=_ANY_ONLY_WSDL)
    schema = _input_schema(spec, "AnyOp")
    payload_ref = schema["properties"]["payload"]["allOf"][0]  # $ref wrapper
    payload = spec["components"]["schemas"][
        payload_ref["$ref"].rsplit("/", 1)[-1]]
    assert "x-soap-simple-content" not in payload
    assert payload.get("additionalProperties") is True
    assert "value" not in payload.get("properties", {})


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
    # XSD patterns implicitly match the whole lexical value; JSON
    # Schema/ECMA-262 'pattern' is an unanchored search by default, so
    # the copied pattern must be anchored to keep XSD semantics (#141 C)
    assert country["pattern"] == "^(?:[A-Z]{2})$"
    assert country["minLength"] == 2 and country["maxLength"] == 2


def test_pattern_facet_is_anchored_for_json_schema_semantics(adv_spec):
    import re

    person = adv_spec["components"]["schemas"]["Person"]
    pattern = person["properties"]["country"]["pattern"]
    # the anchored pattern rejects what the unanchored original would
    # wrongly accept (a match embedded in a longer, invalid string)
    assert re.search(pattern, "xxAAxx") is None
    assert re.search("[A-Z]{2}", "xxAAxx") is not None  # unanchored: false positive
    assert re.search(pattern, "AA") is not None  # a genuinely valid value still matches


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


# -- simple-type wrapper 'value' needs the same x-text annotation (#141 B1) ----

def test_simple_type_wrapper_value_has_x_text_annotation():
    # a wrapper element whose type is a plain simple type (not a
    # complexType) must annotate its "value" property like the
    # simpleContent path does above, or the bridge serializes it as a
    # <value> child element instead of the wrapper element's own text.
    from zeep import xsd as zx

    from spec2openapi.schema import SchemaConverter

    conv = SchemaConverter()
    schema = conv.element_type_to_object_schema(zx.String(), hint="EchoInput")
    assert schema["properties"]["value"]["xml"] == {"x-text": True}
    assert schema["required"] == ["value"]


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
