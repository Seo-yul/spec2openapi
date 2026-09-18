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
