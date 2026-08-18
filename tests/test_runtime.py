"""Regression tests for runtime (bridge/serve/CLI) robustness bugs (#10)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spec2openapi import convert_wsdl
from spec2openapi.bridge import (
    BridgeOptions,
    _choice_violations,
    _coerce,
    _env_bool,
    _env_float,
    _soap_request_headers,
    _soap_version,
    _SpecIndex,
    parse_response,
)
from spec2openapi.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# -- bridge: response parsing ------------------------------------------------

def test_one_way_empty_body_is_success():
    op = {"x-soap": {"soapVersion": "1.1"}}  # no output -> one-way
    assert parse_response(b"", op, None, 202) == (200, {})


def test_http_error_empty_body_reports_status():
    op = {"x-soap": {"soapVersion": "1.1", "output": {"element": "R"}},
          "output": {}}
    status, payload = parse_response(b"", op, None, 401)
    assert status == 502
    assert "401" in payload["faultstring"]
    assert payload["faultcode"] == "spec2openapi.HTTPError"


def test_http_error_html_body_reports_status_not_invalidxml():
    op = {"x-soap": {"soapVersion": "1.1", "output": {"element": "R"}},
          "output": {}}
    status, payload = parse_response(b"<html>503</html>", op, None, 503)
    assert payload["faultcode"] == "spec2openapi.HTTPError"
    assert "503" in payload["faultstring"]


def test_http_error_with_wellformed_nonfault_envelope_not_reported_success():
    """A 4xx/5xx transport status whose body parses as a normal (non-Fault)
    SOAP envelope must not be reported to the model as a 200 success
    (issue #140 F1) — every other empty/non-XML/no-Body branch already
    honors http_status; only this one didn't."""
    op = {"x-soap": {"soapVersion": "1.1", "output": {"element": "R"}},
          "output": {}}
    index = _SpecIndex({})
    resp = (
        b'<soapenv:Envelope '
        b'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        b'<soapenv:Body><R><value>down</value></R></soapenv:Body>'
        b'</soapenv:Envelope>'
    )
    status, payload = parse_response(resp, op, index, 503)
    assert status == 503
    assert "faultstring" in payload
    assert "503" in payload["faultstring"]


# -- bridge: coercion & env --------------------------------------------------

@pytest.mark.parametrize("text,schema,expected", [
    ("42", {"type": ["integer", "null"]}, 42),
    ("3.5", {"type": ["number", "null"]}, 3.5),
    ("true", {"type": ["boolean", "null"]}, True),
    ("false", {"type": ["null", "boolean"]}, False),  # null need not be last
])
def test_coerce_handles_openapi_31_type_arrays(text, schema, expected):
    """to_openapi_31 rewrites nullable scalars to ["type", "null"]; _coerce
    must still coerce instead of returning the raw XML text (#140 F3)."""
    assert _coerce(text, schema) == expected


@pytest.mark.parametrize("text", ["NaN", "INF", "-INF", "Infinity", "-Infinity"])
def test_coerce_number_rejects_non_finite(text):
    """xsd NaN/INF/-INF must not become a non-finite float: httpx's JSON
    encoder emits a bare (invalid-JSON) token for it (#140 F4)."""
    result = _coerce(text, {"type": "number"})
    assert result == text  # left as the original string, not float("nan")
    # must round-trip through strict JSON without raising
    json.dumps({"v": result}, allow_nan=False)


@pytest.mark.parametrize("text,expected", [
    ("true", True), ("TRUE", True), ("1", True),
    ("false", False), ("FALSE", False), ("0", False),
    ("maybe", "maybe"),  # non-canonical: pass through, not silently False
])
def test_boolean_coerce(text, expected):
    assert _coerce(text, {"type": "boolean"}) is expected or \
        _coerce(text, {"type": "boolean"}) == expected


def test_env_float_bad_value_uses_default(monkeypatch):
    monkeypatch.setenv("SPEC2OPENAPI_TIMEOUT", "abc")
    assert _env_float("SPEC2OPENAPI_TIMEOUT", 30.0) == 30.0
    monkeypatch.setenv("SPEC2OPENAPI_TIMEOUT", "")
    assert _env_float("SPEC2OPENAPI_TIMEOUT", 30.0) == 30.0


def test_env_bool_variants(monkeypatch):
    for v in ("FALSE", "no", "off", "0"):
        monkeypatch.setenv("X", v)
        assert _env_bool("X", True) is False
    for v in ("TRUE", "yes", "on", "1"):
        monkeypatch.setenv("X", v)
        assert _env_bool("X", False) is True


def test_env_bool_empty_is_unset_not_false(monkeypatch):
    """An empty value means "leave the default" — it must never turn TLS
    verification off (matching _env_float and the string env vars)."""
    for v in ("", "   "):
        monkeypatch.setenv("X", v)
        assert _env_bool("X", True) is True
        assert _env_bool("X", False) is False
    monkeypatch.setenv("SPEC2OPENAPI_VERIFY", "")
    assert BridgeOptions.from_env().verify is True


# -- bridge: spec indexing ----------------------------------------------------

def _minimal_spec(components):
    return {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": components,
    }


def test_spec_index_null_components_no_crash():
    """components: (no value) parses as None; _SpecIndex must not crash
    the same way cmd_validate used to (#140 F5)."""
    index = _SpecIndex(_minimal_spec(None))
    assert index.components == {}


def test_spec_index_null_component_schemas_no_crash():
    """components: {schemas: (no value)} — same class, one level deeper."""
    index = _SpecIndex(_minimal_spec({"schemas": None}))
    assert index.components == {}


def test_soap_bridge_transport_null_components_no_crash():
    from spec2openapi.bridge import SoapBridgeTransport

    transport = SoapBridgeTransport(_minimal_spec(None),
                                    options=BridgeOptions(trust_env=False))
    assert transport.index.components == {}


# -- bridge: SOAP request framing (soapAction / soapVersion) -----------------

def test_soap_action_null_sends_empty_not_literal_none():
    """soapAction: null (explicit YAML null) must send an empty SOAPAction,
    not the literal string "None" (#140 F6)."""
    headers = _soap_request_headers({"soapVersion": "1.1", "soapAction": None})
    assert headers["SOAPAction"] == '""'


def test_soap_version_yaml_float_uses_12_framing():
    """An unquoted `soapVersion: 1.2` in YAML parses as a Python float, not
    the string "1.2"; it must not silently fall back to 1.1 framing
    (#140 F6)."""
    headers = _soap_request_headers({"soapVersion": 1.2, "soapAction": "Go"})
    assert headers["Content-Type"].startswith("application/soap+xml")
    assert "SOAPAction" not in headers


def test_soap_version_helper_normalizes_float_and_defaults():
    assert _soap_version({"soapVersion": 1.2}) == "1.2"
    assert _soap_version({"soapVersion": "1.2"}) == "1.2"
    assert _soap_version({"soapVersion": None}) == "1.1"
    assert _soap_version({}) == "1.1"


def test_build_envelope_and_parse_response_honor_float_soap_version(orders_wsdl):
    """The same YAML-float soapVersion must not desync build_envelope /
    parse_response's envelope namespace from the outbound Content-Type."""
    from spec2openapi.bridge import build_envelope

    spec = convert_wsdl(orders_wsdl)
    index = _SpecIndex(spec)
    op = dict(index.ops["/operations/CreateOrder"])
    op["x-soap"] = dict(op["x-soap"], soapVersion=1.2)  # YAML float, not "1.2"

    xml = build_envelope(op, {"customer": {"name": "A"}, "items": []},
                         index, BridgeOptions())
    assert b"http://www.w3.org/2003/05/soap-envelope" in xml

    headers = _soap_request_headers(op["x-soap"])
    assert headers["Content-Type"].startswith("application/soap+xml")

    resp = (
        b'<soapenv:Envelope xmlns:soapenv='
        b'"http://www.w3.org/2003/05/soap-envelope">'
        b'<soapenv:Body><soapenv:Fault><soapenv:Code>'
        b'<soapenv:Value>soapenv:Sender</soapenv:Value></soapenv:Code>'
        b'<soapenv:Reason><soapenv:Text>bad</soapenv:Text></soapenv:Reason>'
        b'</soapenv:Fault></soapenv:Body></soapenv:Envelope>'
    )
    status, payload = parse_response(resp, op, index)
    assert status == 500
    assert payload["faultstring"] == "bad"


# -- bridge: choice enforcement ----------------------------------------------

def test_choice_violations():
    sch = {"x-soap-choice": [{"members": ["email", "phone"], "required": True}]}
    assert _choice_violations(sch, {"email": "a", "phone": "b"})  # both -> error
    assert _choice_violations(sch, {})                            # none -> error
    assert _choice_violations(sch, {"email": "a"}) == []          # one -> ok


# -- CLI: error handling -----------------------------------------------------

def test_missing_file_is_clean_error(capsys):
    rc = main(["convert", "/no/such/file.wsdl"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_garbage_spec_is_clean_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("just some text, not a spec\n")
    rc = main(["validate", str(bad)])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_upgrade_non_swagger_is_clean_error(capsys):
    rc = main(["upgrade", str(FIXTURES.parent.parent / "examples"
                              / "orders.openapi.yaml")])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_validate_ignores_path_level_extensions(tmp_path, capsys):
    """A path-item vendor extension must not be read as an operation."""
    import yaml

    spec = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a": {
                "x-meta": {"owner": "team"},  # not an operation
                "get": {"operationId": "getA",
                        "responses": {"200": {"description": "ok"}}},
            }
        },
    }
    f = tmp_path / "s.yaml"
    f.write_text(yaml.safe_dump(spec))
    rc = main(["validate", str(f)])
    out = capsys.readouterr().out
    assert "missing operationId" not in out
    assert rc == 0
