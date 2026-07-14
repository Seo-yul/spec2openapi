"""Hard-error behavior: unconvertible input raises ConversionError (#25)."""
from __future__ import annotations

import logging

import pytest

from spec2openapi import ConversionError, convert_wsdl
from spec2openapi.cli import main as cli_main
from spec2openapi.schema import SchemaConverter

# a WSDL that parses but exposes no SOAP operation (HTTP binding only)
_NOOP_WSDL = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
  xmlns:http="http://schemas.xmlsoap.org/wsdl/http/"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:tns="urn:noop" targetNamespace="urn:noop">
  <message name="m"/>
  <portType name="pt"><operation name="op"><input message="tns:m"/></operation></portType>
  <binding name="b" type="tns:pt">
    <http:binding verb="GET"/>
    <operation name="op"><http:operation location="/op"/></operation>
  </binding>
  <service name="s"><port name="pp" binding="tns:b">
    <http:address location="http://x/y"/></port></service>
</definitions>
"""


def test_conversion_error_is_value_error():
    assert issubclass(ConversionError, ValueError)


def test_zero_operation_wsdl_raises(tmp_path):
    logging.disable(logging.CRITICAL)
    try:
        wsdl = tmp_path / "noop.wsdl"
        wsdl.write_text(_NOOP_WSDL)
        with pytest.raises(ConversionError) as exc:
            convert_wsdl(str(wsdl))
        msg = str(exc.value)
        assert "no convertible SOAP operations" in msg
        assert "skipped" in msg  # rich: lists why nothing converted
    finally:
        logging.disable(logging.NOTSET)


def test_zero_operation_wsdl_cli_clean_error(tmp_path, capsys):
    logging.disable(logging.CRITICAL)
    try:
        wsdl = tmp_path / "noop.wsdl"
        wsdl.write_text(_NOOP_WSDL)
        rc = cli_main(["convert", str(wsdl)])
        assert rc == 2
        assert "error: no convertible SOAP operations" in capsys.readouterr().err
    finally:
        logging.disable(logging.NOTSET)


def test_element_without_type_raises():
    """An element whose XSD type is unresolvable must not be silently dropped."""
    conv = SchemaConverter()

    class _NoType:
        type = None
        name = "mystery"
        qname = None
        nillable = False

    with pytest.raises(ConversionError) as exc:
        conv._element_to_property("mystery", _NoType(), "SomeType", None)
    assert "unresolvable" in str(exc.value)
    assert "mystery" in str(exc.value)
