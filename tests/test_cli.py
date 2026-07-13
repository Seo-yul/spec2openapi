"""CLI behavior tests."""
from __future__ import annotations

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
