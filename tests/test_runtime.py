"""Regression tests for runtime (bridge/serve/CLI) robustness bugs (#10)."""
from __future__ import annotations

from pathlib import Path

import pytest

from spec2openapi.bridge import (
    _choice_violations,
    _coerce,
    _env_bool,
    _env_float,
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


# -- bridge: coercion & env --------------------------------------------------

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
