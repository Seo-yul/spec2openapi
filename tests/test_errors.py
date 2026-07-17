"""Hard-error behavior: unconvertible input raises ConversionError (#25)."""
from __future__ import annotations

import logging

import pytest

from spec2openapi import ConversionError, convert_wsdl, load_spec
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


# -- traceable format errors (#48) -------------------------------------------

def test_json_syntax_error_includes_filename_and_location(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text('{"swagger":"2.0" "info":{}}')  # missing comma
    with pytest.raises(ConversionError) as exc:
        load_spec(str(f))
    msg = str(exc.value)
    assert "bad.json" in msg          # which file
    assert "invalid JSON" in msg
    assert "line 1" in msg            # where


def test_yaml_syntax_error_includes_filename(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("info:\n title: t\n  version: 1\n")  # bad indent
    with pytest.raises(ConversionError) as exc:
        load_spec(str(f))
    msg = str(exc.value)
    assert "bad.yaml" in msg
    assert "invalid YAML" in msg


def test_malformed_wsdl_reports_xml_location(tmp_path):
    logging.disable(logging.CRITICAL)
    try:
        f = tmp_path / "broken.wsdl"
        f.write_text("<definitions><service></definitions>")  # tag mismatch
        with pytest.raises(ConversionError) as exc:
            convert_wsdl(str(f))
        msg = str(exc.value)
        assert "invalid XML" in msg          # not a misleading "no operations"
        assert "broken.wsdl" in msg          # which file
        assert "line" in msg.lower()         # where
    finally:
        logging.disable(logging.NOTSET)


def test_non_xml_wsdl_reports_xml_error_not_cryptic(tmp_path):
    logging.disable(logging.CRITICAL)
    try:
        f = tmp_path / "notxml.wsdl"
        f.write_text("<<<not xml")
        with pytest.raises(ConversionError) as exc:
            convert_wsdl(str(f))
        assert "invalid XML" in str(exc.value)
        assert "getroottree" not in str(exc.value)  # no zeep-internal leak
    finally:
        logging.disable(logging.NOTSET)


# -- library input type guards (#50) -----------------------------------------

@pytest.mark.parametrize("bad", ["a string", None, [1, 2], 42])
def test_convert_swagger_rejects_non_mapping(bad):
    from spec2openapi import convert_swagger
    with pytest.raises(ConversionError) as exc:
        convert_swagger(bad)
    msg = str(exc.value)
    assert "mapping" in msg and type(bad).__name__ in msg


@pytest.mark.parametrize("bad", [{"not": "a path"}, None, 42, ["x"]])
def test_convert_wsdl_rejects_non_path(bad):
    with pytest.raises(ConversionError) as exc:
        convert_wsdl(bad)
    assert type(bad).__name__ in str(exc.value)


def test_is_swagger2_false_for_non_mapping():
    from spec2openapi import is_swagger2
    assert is_swagger2("not a dict") is False
    assert is_swagger2(None) is False
    assert is_swagger2({"swagger": "2.0"}) is True


# -- URL spec input (#69) ------------------------------------------------------

def test_load_spec_accepts_url(monkeypatch):
    """load_spec fetches http(s) sources (no real network: urlopen mocked)."""
    import io
    import contextlib

    payload = b'{"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": {}}'

    @contextlib.contextmanager
    def fake_urlopen(req, timeout=None):
        yield io.BytesIO(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    spec = load_spec("https://example.com/swagger.json")
    assert spec["swagger"] == "2.0"


def test_load_spec_url_fetch_error_is_traceable(monkeypatch):
    from urllib.error import URLError

    def boom(req, timeout=None):
        raise URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ConversionError) as exc:
        load_spec("https://example.com/swagger.json")
    msg = str(exc.value)
    assert "example.com" in msg and "could not fetch" in msg
