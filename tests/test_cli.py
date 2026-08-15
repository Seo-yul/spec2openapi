"""CLI behavior tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from spec2openapi.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_serve_without_mcp_extra_prints_hint(monkeypatch, capsys):
    """serve without the [mcp] extra must hint at the install, not crash."""
    # simulate a core-only install: importing httpx fails, and the cached
    # [mcp] modules are dropped so their imports re-execute
    monkeypatch.setitem(sys.modules, "httpx", None)
    monkeypatch.delitem(sys.modules, "spec2openapi.bridge", raising=False)
    monkeypatch.delitem(sys.modules, "spec2openapi.server", raising=False)

    rc = main(["serve", str(EXAMPLES / "orders.openapi.yaml")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "spec2openapi[mcp]" in err


def test_validate_text_output_ok(capsys):
    rc = main(["validate", str(EXAMPLES / "orders.openapi.yaml")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "operations        :" in out
    assert "OK: spec is FastMCP-convertible" in out


def test_validate_json_output(capsys):
    rc = main(["validate", str(EXAMPLES / "orders.openapi.yaml"),
               "--format", "json"])
    out = capsys.readouterr().out
    assert rc == 0
    report = json.loads(out)
    assert report["ok"] is True
    assert {"pass", "warn", "fail", "skip"} == set(report["summary"])
    ids = {r["id"] for r in report["results"]}
    assert "x-soap.version" in ids and "fastmcp.roundtrip" in ids


def test_validate_json_failure_exit_code(tmp_path, capsys):
    bad = tmp_path / "bad.openapi.yaml"
    bad.write_text(
        "openapi: 3.0.3\n"
        "info: {title: t, version: '1'}\n"
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      responses: {}\n",          # operationId 없음
        encoding="utf-8")
    rc = main(["validate", str(bad), "--format", "json"])
    out = capsys.readouterr().out
    assert rc == 1
    report = json.loads(out)
    assert report["ok"] is False
    assert any(r["id"] == "tool-name.present" and r["status"] == "fail"
               for r in report["results"])
