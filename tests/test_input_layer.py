"""In-memory and multi-file WSDL input: content=, files=, zip (#119)."""
from __future__ import annotations

import io
import zipfile

import pytest

from spec2openapi import ConversionError, convert_wsdl

CALC = "tests/fixtures/calculator.wsdl"

TYPES_XSD = """<?xml version="1.0"?>
<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    targetNamespace="http://example.com/types" elementFormDefault="qualified">
  <xsd:simpleType name="Color">
    <xsd:restriction base="xsd:string">
      <xsd:enumeration value="RED"/><xsd:enumeration value="BLUE"/>
    </xsd:restriction>
  </xsd:simpleType>
  <xsd:element name="PaintRequest"><xsd:complexType><xsd:sequence>
    <xsd:element name="color" type="tns:Color"
        xmlns:tns="http://example.com/types"/>
  </xsd:sequence></xsd:complexType></xsd:element>
  <xsd:element name="PaintResponse"><xsd:complexType><xsd:sequence>
    <xsd:element name="ok" type="xsd:boolean"/>
  </xsd:sequence></xsd:complexType></xsd:element>
</xsd:schema>"""

PAINT_WSDL = """<?xml version="1.0"?>
<wsdl:definitions name="Paint" targetNamespace="http://example.com/paint"
    xmlns:tns="http://example.com/paint" xmlns:t="http://example.com/types"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <wsdl:types><xsd:schema>
    <xsd:import namespace="http://example.com/types"
        schemaLocation="types.xsd"/>
  </xsd:schema></wsdl:types>
  <wsdl:message name="In">
    <wsdl:part name="parameters" element="t:PaintRequest"/></wsdl:message>
  <wsdl:message name="Out">
    <wsdl:part name="parameters" element="t:PaintResponse"/></wsdl:message>
  <wsdl:portType name="P"><wsdl:operation name="Paint">
    <wsdl:input message="tns:In"/><wsdl:output message="tns:Out"/>
  </wsdl:operation></wsdl:portType>
  <wsdl:binding name="B" type="tns:P">
    <soap:binding style="document"
        transport="http://schemas.xmlsoap.org/soap/http"/>
    <wsdl:operation name="Paint"><soap:operation soapAction="urn:paint"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input>
      <wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation></wsdl:binding>
  <wsdl:service name="PaintService"><wsdl:port name="P" binding="tns:B">
    <soap:address location="http://example.com/paint"/></wsdl:port>
  </wsdl:service>
</wsdl:definitions>"""


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("service.wsdl", PAINT_WSDL)
        z.writestr("types.xsd", TYPES_XSD)
    return buf.getvalue()


def test_content_matches_path_conversion():
    base = convert_wsdl(CALC)
    text = open(CALC).read()
    for doc in (convert_wsdl(content=text),
                convert_wsdl(content=text.encode())):
        assert doc["paths"].keys() == base["paths"].keys()
        assert doc["x-soap"]["wsdl"] == "<memory>"


def test_files_bundle_resolves_relative_imports_offline():
    spec = convert_wsdl(
        files={"service.wsdl": PAINT_WSDL, "types.xsd": TYPES_XSD},
        forbid_external=True,  # bundle-internal imports must still work
    )
    assert spec["x-soap"]["wsdl"] == "<bundle:service.wsdl>"
    color = spec["paths"]["/operations/Paint"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]["properties"]["color"]
    # the imported XSD's facets survived the temp-dir materialization
    assert color["enum"] == ["RED", "BLUE"] and color["example"] == "RED"


def test_zip_bytes_and_zip_path(tmp_path):
    zb = _zip_bytes()
    spec = convert_wsdl(content=zb)
    assert spec["x-soap"]["wsdl"] == "<memory>!service.wsdl"

    zp = tmp_path / "bundle.zip"
    zp.write_bytes(zb)
    spec2 = convert_wsdl(str(zp))
    assert spec2["x-soap"]["wsdl"] == f"{zp}!service.wsdl"
    assert spec2["paths"].keys() == spec["paths"].keys()


@pytest.mark.parametrize("label,call", [
    ("both inputs", lambda: convert_wsdl(CALC, content="x")),
    ("no input", lambda: convert_wsdl()),
    ("bad content type", lambda: convert_wsdl(content=123)),
    ("unsafe member name",
     lambda: convert_wsdl(files={"../evil.xsd": "x", "a.wsdl": PAINT_WSDL})),
    ("absolute member name",
     lambda: convert_wsdl(files={"/abs.xsd": "x", "a.wsdl": PAINT_WSDL})),
    ("ambiguous entry",
     lambda: convert_wsdl(files={"a.wsdl": PAINT_WSDL,
                                 "b.wsdl": PAINT_WSDL})),
    ("entry not in bundle",
     lambda: convert_wsdl(files={"service.wsdl": PAINT_WSDL},
                          entry="nope.wsdl")),
    ("entry with plain path", lambda: convert_wsdl(CALC, entry="x")),
    ("empty files", lambda: convert_wsdl(files={})),
])
def test_input_violations_are_conversion_errors(label, call):
    with pytest.raises(ConversionError):
        call()


def test_cli_stdin(tmp_path, monkeypatch, capsys):
    from spec2openapi.cli import main as cli_main

    class _Stdin:
        buffer = io.BytesIO(open(CALC, "rb").read())

    monkeypatch.setattr("sys.stdin", _Stdin())
    out = tmp_path / "out.yaml"
    assert cli_main(["convert", "-", "-o", str(out)]) == 0
    assert "operations" in out.read_text()
