"""#159: defects from a full review of the post-0.8.0 code."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from spec2openapi import ConversionError, convert_swagger, convert_wsdl, load_spec
from spec2openapi.bridge import (
    BridgeOptions,
    _SpecIndex,
    build_envelope,
    parse_response,
)
from spec2openapi.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _wsdl(types: str) -> str:
    return f"""<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
  xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
  xmlns:tns="urn:t" xmlns:b="urn:b"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  targetNamespace="urn:t" name="S">
  <types>{types}</types>
  <message name="In"><part name="p" element="tns:Req"/></message>
  <message name="Out"><part name="p" element="tns:Resp"/></message>
  <portType name="PT"><operation name="Op"><input message="tns:In"/>
    <output message="tns:Out"/></operation></portType>
  <binding name="B" type="tns:PT">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="Op"><soap:operation soapAction="urn:op"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output></operation></binding>
  <service name="S"><port name="P" binding="tns:B">
    <soap:address location="http://x/s"/></port></service>
</definitions>"""


def _req_props(spec: dict) -> dict:
    op = spec["paths"]["/operations/Op"]["post"]
    return op["requestBody"]["content"]["application/json"]["schema"][
        "properties"]


def _envelope(body: str) -> bytes:
    return (b'<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/">'
            b"<e:Body>" + body.encode() + b"</e:Body></e:Envelope>")


# --- 1. SOAP bridge on 3.1 nullables ----------------------------------------

def _nil_types(occurs: str = "") -> str:
    return f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  <xsd:complexType name="Addr"><xsd:sequence>
    <xsd:element name="city" type="xsd:string"/></xsd:sequence></xsd:complexType>
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="addr" type="tns:Addr" nillable="true"{occurs}/>
  </xsd:sequence></xsd:complexType></xsd:element>
  <xsd:element name="Resp"><xsd:complexType><xsd:sequence>
    <xsd:element name="addr" type="tns:Addr" nillable="true"{occurs}/>
  </xsd:sequence></xsd:complexType></xsd:element>
</xsd:schema>"""


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_nillable_complex_element_round_trips(version):
    spec = convert_wsdl(content=_wsdl(_nil_types()), openapi_version=version)
    idx = _SpecIndex(spec)
    op = idx.ops["/operations/Op"]
    env = build_envelope(op, {"addr": {"city": "Seoul"}}, idx,
                         BridgeOptions()).decode()
    assert "<tns:addr><tns:city>Seoul</tns:city></tns:addr>" in env
    resp = _envelope('<t:Resp xmlns:t="urn:t"><t:addr><t:city>Seoul'
                     "</t:city></t:addr></t:Resp>")
    assert parse_response(resp, op, idx) == (200, {"addr": {"city": "Seoul"}})


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_nillable_repeated_complex_element_round_trips(version):
    spec = convert_wsdl(content=_wsdl(_nil_types(' maxOccurs="unbounded"')),
                        openapi_version=version)
    idx = _SpecIndex(spec)
    op = idx.ops["/operations/Op"]
    env = build_envelope(op, {"addr": [{"city": "A"}, {"city": "B"}]}, idx,
                         BridgeOptions()).decode()
    assert ("<tns:addr><tns:city>A</tns:city></tns:addr>"
            "<tns:addr><tns:city>B</tns:city></tns:addr>") in env
    resp = _envelope('<t:Resp xmlns:t="urn:t"><t:addr><t:city>A</t:city>'
                     "</t:addr><t:addr><t:city>B</t:city></t:addr></t:Resp>")
    assert parse_response(resp, op, idx) == (
        200, {"addr": [{"city": "A"}, {"city": "B"}]})


# --- 2-4. XSD lookups and facets ---------------------------------------------

_RESP = """<xsd:element name="Resp"><xsd:complexType><xsd:sequence>
  <xsd:element name="ok" type="xsd:string"/></xsd:sequence></xsd:complexType>
</xsd:element>"""


def test_a_facetless_type_does_not_borrow_another_namespaces_facets():
    types = f"""<xsd:schema targetNamespace="urn:b">
  <xsd:simpleType name="Status"><xsd:restriction base="xsd:string">
    <xsd:enumeration value="OPEN"/><xsd:enumeration value="CLOSED"/>
  </xsd:restriction></xsd:simpleType>
</xsd:schema>
<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  <xsd:simpleType name="Status"><xsd:restriction base="xsd:string"/>
  </xsd:simpleType>
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="s" type="tns:Status"/></xsd:sequence></xsd:complexType>
  </xsd:element>{_RESP}
</xsd:schema>"""
    s = _req_props(convert_wsdl(content=_wsdl(types)))["s"]
    assert "enum" not in s


_CODE_TYPES = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  <xsd:simpleType name="BaseInt"><xsd:restriction base="xsd:int"/></xsd:simpleType>
  <xsd:simpleType name="Code"><xsd:restriction base="tns:BaseInt">
    <xsd:enumeration value="1"/><xsd:enumeration value="2"/>
  </xsd:restriction></xsd:simpleType>
  <xsd:simpleType name="Amt"><xsd:restriction base="xsd:decimal">
    <xsd:fractionDigits value="2"/></xsd:restriction></xsd:simpleType>
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="s" type="tns:Code"/><xsd:element name="a" type="tns:Amt"/>
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""


def test_enum_on_a_restriction_of_a_user_integer_type_is_typed():
    s = _req_props(convert_wsdl(content=_wsdl(_CODE_TYPES)))["s"]
    assert s["type"] == "integer"
    assert s["enum"] == [1, 2]
    assert s["example"] == 1


def test_fraction_digits_are_kept_without_a_float_multiple_of():
    a = _req_props(convert_wsdl(content=_wsdl(_CODE_TYPES)))["a"]
    assert "multipleOf" not in a
    assert a["x-fractionDigits"] == 2


# --- 5, 6, 9-13. Swagger upgrader --------------------------------------------

def _sw(paths=None, **extra):
    return {"swagger": "2.0", "info": {"title": "t", "version": "1"},
            "paths": paths or {}, **extra}


_OK = {"200": {"description": "ok"}}


def test_multipart_consumes_is_honoured_without_a_file_field():
    out = convert_swagger(_sw({"/a": {"post": {
        "operationId": "a", "consumes": ["multipart/form-data"],
        "parameters": [{"in": "formData", "name": "f", "type": "string"}],
        "responses": _OK}}}))
    assert list(out["paths"]["/a"]["post"]["requestBody"]["content"]) == [
        "multipart/form-data"]


def test_null_values_in_response_examples_are_kept():
    out = convert_swagger(_sw({"/a": {"get": {
        "operationId": "a", "produces": ["application/json"],
        "responses": {"200": {"description": "ok",
                              "schema": {"type": "object"},
                              "examples": {"application/json":
                                           {"name": None, "id": 1}}}}}}}))
    media = out["paths"]["/a"]["get"]["responses"]["200"]["content"][
        "application/json"]
    assert media["example"] == {"name": None, "id": 1}


def test_a_ref_body_parameter_follows_the_operations_consumes():
    out = convert_swagger(_sw(
        {"/a": {"post": {"operationId": "a", "consumes": ["application/xml"],
                         "parameters": [{"$ref": "#/parameters/B"}],
                         "responses": _OK}}},
        parameters={"B": {"in": "body", "name": "b",
                          "schema": {"type": "object"}}}))
    body = out["paths"]["/a"]["post"]["requestBody"]
    assert list(body["content"]) == ["application/xml"]
    assert any("'B'" in n and "consumes" in n
               for n in out["x-s2o"]["assumptions"])


def test_string_false_required_on_body_and_form_parameters():
    out = convert_swagger(_sw({
        "/a": {"post": {"operationId": "a", "parameters": [
            {"in": "body", "name": "b", "required": "false",
             "schema": {"type": "object"}}], "responses": _OK}},
        "/f": {"post": {"operationId": "f", "parameters": [
            {"in": "formData", "name": "x", "type": "string",
             "required": "false"}], "responses": _OK}}}))
    assert not out["paths"]["/a"]["post"]["requestBody"].get("required")
    form = out["paths"]["/f"]["post"]["requestBody"]
    assert not form["required"]
    schema = form["content"]["application/x-www-form-urlencoded"]["schema"]
    assert "required" not in schema


def test_a_property_named_like_a_data_keyword_is_still_a_schema():
    out = convert_swagger(_sw(definitions={"Foo": {
        "type": "object",
        "properties": {"default": {"type": "string", "format": None},
                       "enum": {"type": "string", "format": None}}}}))
    props = out["components"]["schemas"]["Foo"]["properties"]
    assert props["default"] == {"type": "string"}
    assert props["enum"] == {"type": "string"}


def test_a_non_mapping_response_is_a_conversion_error():
    with pytest.raises(ConversionError, match="200"):
        convert_swagger(_sw({"/a": {"get": {
            "operationId": "a", "responses": {"200": "OK"}}}}))


def test_a_numeric_operation_id_is_used_as_text():
    out = convert_swagger(_sw({"/a": {"get": {
        "operationId": 123, "responses": _OK}}}))
    assert out["paths"]["/a"]["get"]["operationId"] == "123"
    assert any("123" in n for n in out["x-s2o"]["assumptions"])


def test_a_structured_operation_id_is_a_conversion_error():
    with pytest.raises(ConversionError, match="operationId"):
        convert_swagger(_sw({"/a": {"get": {
            "operationId": ["x"], "responses": _OK}}}))


# --- 7, 8, 14. Input detection ------------------------------------------------

_OPENAPI_DOC = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                "paths": {"/a": {"get": {
                    "operationId": "getA", "summary": "s",
                    "responses": {"200": {"description": "ok"}}}}}}


def test_an_extensionless_url_serving_openapi_is_read_as_a_spec(
        monkeypatch, capsys):
    monkeypatch.setattr("spec2openapi.convert._fetch_url",
                        lambda src: json.dumps(_OPENAPI_DOC).encode())
    rc = main(["validate", "https://host.invalid/v3/api-docs"])
    assert rc == 0, capsys.readouterr().err


def test_an_extensionless_url_serving_xml_is_read_as_wsdl(monkeypatch):
    seen = []
    monkeypatch.setattr("spec2openapi.convert._fetch_url",
                        lambda src: b"<?xml version='1.0'?><definitions/>")

    def fake_convert_wsdl(source, **kw):
        seen.append(source)
        return _OPENAPI_DOC
    monkeypatch.setattr("spec2openapi.convert.convert_wsdl", fake_convert_wsdl)
    assert main(["validate", "https://host.invalid/service"]) == 0
    assert seen == ["https://host.invalid/service"]


def test_validate_accepts_a_zip_bundle(tmp_path, capsys):
    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.write(FIXTURES / "calculator.wsdl", "calculator.wsdl")
    rc = main(["validate", str(bundle)])
    assert rc == 0, capsys.readouterr().err


def test_flow_style_yaml_starting_with_a_brace_loads(tmp_path):
    src = tmp_path / "flow.yaml"
    src.write_text('{openapi: 3.0.3, info: {title: t, version: "1"}, '
                   "paths: {}}")
    assert load_spec(src)["openapi"] == "3.0.3"


# --- cycle 2 ------------------------------------------------------------------

def test_a_null_property_or_definition_is_dropped():
    out = convert_swagger(_sw(definitions={
        "X": {"type": "object",
              "properties": {"foo": None, "bar": {"type": "string"}}},
        "Y": None}))
    schemas = out["components"]["schemas"]
    assert schemas["X"]["properties"] == {"bar": {"type": "string"}}
    assert "Y" not in schemas


@pytest.mark.parametrize("owner", ["team-a", {"team": "a"}])
def test_vendor_extensions_on_responses_are_kept_not_converted(owner):
    out = convert_swagger(_sw({"/a": {"get": {
        "operationId": "a",
        "responses": {"200": {"description": "ok"}, "x-owner": owner}}}}))
    responses = out["paths"]["/a"]["get"]["responses"]
    assert responses["x-owner"] == owner
    assert set(responses) == {"200", "x-owner"}


def test_refs_under_names_that_look_like_data_keywords_are_checked():
    from spec2openapi import verify

    xsoap = {"soapVersion": "1.1", "endpoint": "http://e",
             "input": {"element": "In"}, "output": {"element": "Out"}}
    missing = "#/components/schemas/Missing"
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/operations/Op": {"post": {
                "operationId": "Op", "summary": "s", "x-soap": xsoap,
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {
                        "default": {"$ref": missing + "1"},
                        "enum": {"$ref": missing + "2"}}}}}},
                "responses": {"default": {
                    "description": "d", "content": {"application/json": {
                        "schema": {"$ref": missing + "3"}}}}}}}},
            "components": {"schemas": {}}}
    messages = " ".join(r.message for r in verify(spec, deep=False).results
                        if r.id == "x-soap.refs")
    for n in "123":
        assert missing + n in messages


def test_kana_makes_kanji_heavy_text_japanese():
    from spec2openapi.agentize.targets import detect_language

    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {}, "components": {"schemas": {"O": {
                "type": "object", "properties": {
                    k: {"type": "string", "description": d} for k, d in (
                        ("a", "顧客番号"), ("b", "注文日時"),
                        ("c", "商品コード"), ("d", "受注金額の合計"))}}}}}
    assert detect_language(spec) == "Japanese"


# --- cycle 3 ------------------------------------------------------------------

@pytest.mark.parametrize("named_ns", ["urn:t", "urn:b"])
def test_an_inline_simple_type_keeps_its_own_facets(named_ns):
    """zeep names an anonymous simpleType after its element, so its qname
    can equal a named type's - it must not borrow that type's facets."""
    prefix = "tns" if named_ns == "urn:t" else "b"
    named = """<xsd:simpleType name="code"><xsd:restriction base="xsd:string">
    <xsd:enumeration value="USD"/><xsd:enumeration value="EUR"/>
  </xsd:restriction></xsd:simpleType>"""
    other = (f'<xsd:schema targetNamespace="urn:b">{named}</xsd:schema>'
             if named_ns == "urn:b" else "")
    types = f"""{other}<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  {named if named_ns == "urn:t" else ""}
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="code"><xsd:simpleType><xsd:restriction base="xsd:string">
      <xsd:maxLength value="10"/></xsd:restriction></xsd:simpleType></xsd:element>
    <xsd:element name="cur" type="{prefix}:code"/>
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    props = _req_props(convert_wsdl(content=_wsdl(types)))
    assert "enum" not in props["code"]
    assert props["code"]["maxLength"] == 10
    assert "x-soap-simple-type" not in props["code"]
    assert props["cur"]["enum"] == ["USD", "EUR"]


def test_decimal_values_are_written_without_an_exponent():
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="rate" type="xsd:decimal"/>
    <xsd:element name="big" type="xsd:decimal"/>
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    spec = convert_wsdl(content=_wsdl(types))
    idx = _SpecIndex(spec)
    env = build_envelope(idx.ops["/operations/Op"],
                         {"rate": 0.00001, "big": 1e16}, idx,
                         BridgeOptions()).decode()
    assert "<tns:rate>0.00001</tns:rate>" in env
    assert "<tns:big>10000000000000000</tns:big>" in env


def test_a_ref_response_follows_the_operations_produces():
    out = convert_swagger(_sw(
        {"/a": {"get": {"operationId": "a", "produces": ["application/xml"],
                        "responses": {
                            "200": {"description": "ok",
                                    "schema": {"type": "string"}},
                            "400": {"$ref": "#/responses/Err"}}}}},
        produces=["application/json"],
        responses={"Err": {"description": "e",
                           "schema": {"type": "string"}}}))
    r400 = out["paths"]["/a"]["get"]["responses"]["400"]
    assert list(r400["content"]) == ["application/xml"]
    assert any("'Err'" in n and "produces" in n
               for n in out["x-s2o"]["assumptions"])


def test_extension_only_responses_still_get_a_response_code():
    out = convert_swagger(_sw({"/a": {"get": {
        "operationId": "a", "responses": {"x-note": "hi"}}}}))
    responses = out["paths"]["/a"]["get"]["responses"]
    assert responses["x-note"] == "hi"
    assert "200" in responses


def test_non_mapping_response_headers_are_a_conversion_error():
    with pytest.raises(ConversionError, match="headers"):
        convert_swagger(_sw({"/a": {"get": {"operationId": "a", "responses": {
            "200": {"description": "ok", "headers": ["X"]}}}}}))


def test_a_stray_katakana_mark_does_not_make_chinese_japanese():
    from spec2openapi.agentize.targets import detect_language

    spec = {"info": {"description":
                     "查询用户订单信息，返回订单列表。支持分页查询。迈克尔・杰克逊"}}
    assert detect_language(spec) == "Chinese"


# --- cycle 3 follow-up: where an inline simpleType is recorded -----------------

_CODE_NAMED = """<xsd:simpleType name="code"><xsd:restriction base="xsd:string">
    <xsd:enumeration value="USD"/><xsd:enumeration value="EUR"/>
  </xsd:restriction></xsd:simpleType>"""
_INLINE_CODE = """<xsd:element name="code"><xsd:simpleType>
      <xsd:restriction base="xsd:string"><xsd:maxLength value="3"/>
      </xsd:restriction></xsd:simpleType></xsd:element>"""


@pytest.mark.parametrize("outer", ["xsd:int", "tns:code"])
def test_a_nested_inline_type_does_not_leak_to_a_same_named_outer_element(
        outer):
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  {_CODE_NAMED}
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="inner"><xsd:complexType><xsd:sequence>
      {_INLINE_CODE}
    </xsd:sequence></xsd:complexType></xsd:element>
    <xsd:element name="code" type="{outer}"/>
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    spec = convert_wsdl(content=_wsdl(types))
    outer_code = _req_props(spec)["code"]
    assert "maxLength" not in outer_code
    if outer == "tns:code":
        assert outer_code["enum"] == ["USD", "EUR"]
    inner = _req_props(spec)["inner"]
    ref = (inner.get("allOf") or [inner])[0].get("$ref")
    inner_schema = (spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
                    if ref else inner)
    inner_code = inner_schema["properties"]["code"]
    assert inner_code["maxLength"] == 3 and "enum" not in inner_code


@pytest.mark.parametrize("via", ["extension", "group"])
def test_an_inherited_inline_type_keeps_its_own_facets(via):
    if via == "extension":
        decl = f"""<xsd:complexType name="Base"><xsd:sequence>{_INLINE_CODE}
  </xsd:sequence></xsd:complexType>
  <xsd:complexType name="Derived"><xsd:complexContent>
    <xsd:extension base="tns:Base"><xsd:sequence>
      <xsd:element name="extra" type="xsd:string"/>
    </xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType>"""
    else:
        decl = f"""<xsd:group name="G"><xsd:sequence>{_INLINE_CODE}
  </xsd:sequence></xsd:group>
  <xsd:complexType name="Derived"><xsd:sequence>
    <xsd:group ref="tns:G"/>
    <xsd:element name="extra" type="xsd:string"/>
  </xsd:sequence></xsd:complexType>"""
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  {_CODE_NAMED}
  {decl}
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="d" type="tns:Derived"/>
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    derived = convert_wsdl(content=_wsdl(types))["components"]["schemas"][
        "Derived"]
    props = derived.get("properties") or {}
    for part in derived.get("allOf", []):
        props = {**props, **(part.get("properties") or {})}
    code = props["code"]
    assert code["maxLength"] == 3 and "enum" not in code


def _component_of(spec, schema):
    ref = (schema.get("allOf") or [schema])[0].get("$ref")
    return spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]


def _inline(name, facet):
    return (f'<xsd:element name="{name}"><xsd:simpleType>'
            f'<xsd:restriction base="xsd:string">{facet}</xsd:restriction>'
            "</xsd:simpleType></xsd:element>")


def test_same_named_local_containers_do_not_share_inline_facets():
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
<xsd:complexType name="Cust"><xsd:sequence>
 <xsd:element name="Item"><xsd:complexType><xsd:sequence>
  {_inline("code", '<xsd:maxLength value="3"/>')}
 </xsd:sequence></xsd:complexType></xsd:element>
</xsd:sequence></xsd:complexType>
<xsd:complexType name="Supp"><xsd:sequence>
 <xsd:element name="Item"><xsd:complexType><xsd:sequence>
  <xsd:element name="code" type="xsd:string"/>
 </xsd:sequence></xsd:complexType></xsd:element>
</xsd:sequence></xsd:complexType>
<xsd:element name="Req"><xsd:complexType><xsd:sequence>
 <xsd:element name="c" type="tns:Cust"/><xsd:element name="s" type="tns:Supp"/>
</xsd:sequence></xsd:complexType></xsd:element>{_RESP}</xsd:schema>"""
    spec = convert_wsdl(content=_wsdl(types))
    supp = spec["components"]["schemas"]["Supp"]
    code = _component_of(spec, supp["properties"]["Item"])["properties"]["code"]
    assert "maxLength" not in code


def test_a_global_type_does_not_take_a_same_named_local_elements_facets():
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
<xsd:complexType name="Cust"><xsd:sequence>
 <xsd:element name="Item"><xsd:complexType><xsd:sequence>
  {_inline("code", '<xsd:enumeration value="A"/><xsd:enumeration value="B"/>')}
 </xsd:sequence></xsd:complexType></xsd:element>
</xsd:sequence></xsd:complexType>
<xsd:complexType name="Item"><xsd:sequence>
 <xsd:element name="code" type="xsd:string"/></xsd:sequence></xsd:complexType>
<xsd:element name="Req"><xsd:complexType><xsd:sequence>
 <xsd:element name="c" type="tns:Cust"/><xsd:element name="i" type="tns:Item"/>
</xsd:sequence></xsd:complexType></xsd:element>{_RESP}</xsd:schema>"""
    spec = convert_wsdl(content=_wsdl(types))
    item = _component_of(spec, _req_props(spec)["i"])
    assert "enum" not in item["properties"]["code"]


def test_group_and_type_names_are_separate():
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
<xsd:group name="Addr"><xsd:sequence>
 {_inline("zip", '<xsd:maxLength value="5"/>')}</xsd:sequence></xsd:group>
<xsd:complexType name="Addr"><xsd:sequence>
 <xsd:element name="zip" type="xsd:int"/></xsd:sequence></xsd:complexType>
<xsd:complexType name="Home"><xsd:sequence>
 <xsd:group ref="tns:Addr"/></xsd:sequence></xsd:complexType>
<xsd:element name="Req"><xsd:complexType><xsd:sequence>
 <xsd:element name="h" type="tns:Home"/><xsd:element name="a" type="tns:Addr"/>
</xsd:sequence></xsd:complexType></xsd:element>{_RESP}</xsd:schema>"""
    spec = convert_wsdl(content=_wsdl(types))
    addr_zip = _component_of(spec, _req_props(spec)["a"])["properties"]["zip"]
    assert addr_zip["type"] == "integer" and "maxLength" not in addr_zip
    home_zip = _component_of(spec, _req_props(spec)["h"])["properties"]["zip"]
    assert home_zip["maxLength"] == 5


# --- #161: anonymous simpleTypes are told apart without names ------------------

def test_the_zeep_hook_marks_anonymous_simple_types():
    """The hook wraps zeep's private SchemaVisitor dispatch; if a zeep
    upgrade changes it, this fails first (conversion then falls back to
    the name lookup)."""
    from spec2openapi.parser import install_anonymous_type_hook, parse_wsdl

    assert install_anonymous_type_hook()
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  {_CODE_NAMED}
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    {_INLINE_CODE}<xsd:element name="named" type="tns:code"/>
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    parsed = parse_wsdl(content=_wsdl(types))
    elements = dict(parsed.operations[0].input_element.type.elements)
    assert getattr(elements["code"].type, "_s2o_simple_node", None) is not None
    assert getattr(elements["named"].type, "_s2o_simple_node", None) is None


def test_an_anonymous_simple_type_on_an_attribute_keeps_its_facets():
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  {_CODE_NAMED}
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="v" type="xsd:string"/></xsd:sequence>
    <xsd:attribute name="code"><xsd:simpleType>
      <xsd:restriction base="xsd:string"><xsd:maxLength value="4"/>
      </xsd:restriction></xsd:simpleType></xsd:attribute>
  </xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    code = _req_props(convert_wsdl(content=_wsdl(types)))["code"]
    assert code["maxLength"] == 4 and "enum" not in code


# --- #161 review: an anonymous type's facets follow its restriction chain -----

def _one_prop(element_xml: str, extra: str = "") -> dict:
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  {extra}
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    {element_xml}
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    props = _req_props(convert_wsdl(content=_wsdl(types)))
    assert len(props) == 1
    return next(iter(props.values()))


def test_a_baseless_restriction_takes_its_inner_types_kind():
    n = _one_prop("""<xsd:element name="n"><xsd:simpleType><xsd:restriction>
      <xsd:simpleType><xsd:restriction base="xsd:int"/></xsd:simpleType>
      <xsd:enumeration value="1"/><xsd:enumeration value="2"/>
    </xsd:restriction></xsd:simpleType></xsd:element>""")
    assert n["type"] == "integer" and n["enum"] == [1, 2] and n["example"] == 1


def test_a_chameleon_included_base_is_followed():
    spec = convert_wsdl(str(FIXTURES / "chameleon" / "service.wsdl"))
    n = _req_props(spec)["n"]
    assert n["type"] == "integer" and n["enum"] == [1, 2]


@pytest.mark.parametrize("base", ["inner", "named"])
def test_list_length_facets_are_not_string_lengths(base):
    if base == "inner":
        element = """<xsd:element name="ids"><xsd:simpleType><xsd:restriction>
      <xsd:simpleType><xsd:list itemType="xsd:int"/></xsd:simpleType>
      <xsd:maxLength value="3"/></xsd:restriction></xsd:simpleType></xsd:element>"""
        extra = ""
    else:
        element = """<xsd:element name="ids"><xsd:simpleType>
      <xsd:restriction base="tns:IdList"><xsd:maxLength value="3"/>
      </xsd:restriction></xsd:simpleType></xsd:element>"""
        extra = ('<xsd:simpleType name="IdList"><xsd:list itemType="xsd:int"/>'
                 "</xsd:simpleType>")
    ids = _one_prop(element, extra)
    assert "maxLength" not in ids and "minLength" not in ids


def test_a_restriction_of_a_named_type_inherits_its_facets():
    cur = _one_prop(
        """<xsd:element name="cur"><xsd:simpleType>
      <xsd:restriction base="tns:cur"><xsd:pattern value="[A-Z]+"/>
      </xsd:restriction></xsd:simpleType></xsd:element>""",
        """<xsd:simpleType name="cur"><xsd:restriction base="xsd:string">
      <xsd:enumeration value="USD"/><xsd:enumeration value="EUR"/>
      </xsd:restriction></xsd:simpleType>""")
    assert cur["enum"] == ["USD", "EUR"]
    assert cur["pattern"] == "^(?:[A-Z]+)$"


def test_a_nested_restriction_keeps_the_inner_facets():
    v = _one_prop("""<xsd:element name="v"><xsd:simpleType><xsd:restriction>
      <xsd:simpleType><xsd:restriction base="xsd:string">
        <xsd:maxLength value="5"/></xsd:restriction></xsd:simpleType>
      <xsd:pattern value="[a-z]+"/></xsd:restriction></xsd:simpleType>
      </xsd:element>""")
    assert v["maxLength"] == 5 and v["pattern"] == "^(?:[a-z]+)$"


@pytest.mark.parametrize("element_doc, expected", [
    ("element doc", "element doc"), (None, "type doc")])
def test_an_elements_own_documentation_wins_over_its_types(element_doc,
                                                            expected):
    ann = (f"<xsd:annotation><xsd:documentation>{element_doc}"
           "</xsd:documentation></xsd:annotation>" if element_doc else "")
    d = _one_prop(f"""<xsd:element name="d">{ann}<xsd:simpleType>
      <xsd:annotation><xsd:documentation>type doc</xsd:documentation>
      </xsd:annotation><xsd:restriction base="xsd:string"/>
      </xsd:simpleType></xsd:element>""")
    assert d["description"] == expected


# --- #161 review round 2 --------------------------------------------------------

@pytest.mark.parametrize("base_facet, own_facet, bound, value", [
    ('<xsd:minExclusive value="0"/>', '<xsd:minInclusive value="10"/>',
     "minimum", 10),
    ('<xsd:maxExclusive value="100"/>', '<xsd:maxInclusive value="50"/>',
     "maximum", 50)])
def test_an_inclusive_bound_replaces_an_inherited_exclusive_one(
        base_facet, own_facet, bound, value):
    flag = "exclusiveMinimum" if bound == "minimum" else "exclusiveMaximum"
    n = _one_prop(
        f"""<xsd:element name="n"><xsd:simpleType>
      <xsd:restriction base="tns:Bounded">{own_facet}</xsd:restriction>
      </xsd:simpleType></xsd:element>""",
        f"""<xsd:simpleType name="Bounded"><xsd:restriction base="xsd:int">
      {base_facet}</xsd:restriction></xsd:simpleType>""")
    assert n[bound] == value
    assert flag not in n


@pytest.mark.parametrize("builtin", ["NMTOKENS", "IDREFS", "ENTITIES"])
def test_builtin_list_types_drop_length_facets(builtin):
    toks = _one_prop(f"""<xsd:element name="toks"><xsd:simpleType>
      <xsd:restriction base="xsd:{builtin}"><xsd:maxLength value="2"/>
      </xsd:restriction></xsd:simpleType></xsd:element>""")
    assert "maxLength" not in toks


def test_an_included_list_base_drops_length_facets():
    spec = convert_wsdl(str(FIXTURES / "chameleon_list" / "service.wsdl"))
    ids = _req_props(spec)["ids"]
    assert "maxLength" not in ids and "minLength" not in ids


def test_boolean_enumerations_are_booleans():
    b = _one_prop("""<xsd:element name="b"><xsd:simpleType>
      <xsd:restriction base="xsd:boolean"><xsd:enumeration value="true"/>
      </xsd:restriction></xsd:simpleType></xsd:element>""")
    assert b["type"] == "boolean" and b["enum"] == [True] and b["example"] is True
    named = _one_prop(
        '<xsd:element name="nb" type="tns:Flag"/>',
        """<xsd:simpleType name="Flag"><xsd:restriction base="xsd:boolean">
      <xsd:enumeration value="1"/></xsd:restriction></xsd:simpleType>""")
    assert named["enum"] == [True]


# --- #161 review round 3 --------------------------------------------------------

def test_an_unresolved_base_does_not_borrow_a_no_namespace_type():
    """A base missing from the scanned documents (xsd:include merges them
    into the includer) must not resolve to a same-named no-namespace type:
    that is an unrelated type as far as the lookup can tell."""
    from lxml import etree

    from spec2openapi.parser import XSD_NS, _named_simple_type

    index = {("", "Code"): etree.fromstring(
        f'<simpleType xmlns="{XSD_NS}" name="Code"/>')}
    assert _named_simple_type(index, ("urn:t", "Code")) is None
    assert _named_simple_type(index, ("", "Code")) is not None


def test_patterns_in_one_restriction_are_alternatives():
    v = _one_prop("""<xsd:element name="v"><xsd:simpleType>
      <xsd:restriction base="xsd:string">
        <xsd:pattern value="[A-Z]+"/><xsd:pattern value="[0-9]+"/>
      </xsd:restriction></xsd:simpleType></xsd:element>""")
    import re
    assert re.fullmatch(v["pattern"], "ABC") and re.fullmatch(v["pattern"], "123")
    assert not re.fullmatch(v["pattern"], "abc")


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_a_nillable_enumeration_accepts_null(version):
    types = f"""<xsd:schema targetNamespace="urn:t" elementFormDefault="qualified">
  <xsd:element name="Req"><xsd:complexType><xsd:sequence>
    <xsd:element name="c" nillable="true"><xsd:simpleType>
      <xsd:restriction base="xsd:string">
        <xsd:enumeration value="A"/><xsd:enumeration value="B"/>
      </xsd:restriction></xsd:simpleType></xsd:element>
  </xsd:sequence></xsd:complexType></xsd:element>{_RESP}
</xsd:schema>"""
    c = _req_props(convert_wsdl(content=_wsdl(types), openapi_version=version))["c"]
    assert None in c["enum"]


def test_non_finite_values_are_not_emitted():
    d = _one_prop("""<xsd:element name="d"><xsd:simpleType>
      <xsd:restriction base="xsd:double">
        <xsd:enumeration value="INF"/><xsd:enumeration value="1.0"/>
        <xsd:maxInclusive value="INF"/>
      </xsd:restriction></xsd:simpleType></xsd:element>""")
    import math
    for key in ("enum", "maximum", "example"):
        values = d.get(key)
        values = values if isinstance(values, list) else [values]
        assert all(not isinstance(x, float) or math.isfinite(x)
                   for x in values if x is not None), (key, d)
