from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
sys.path.insert(0, str(TESTS))

from mock_soap_server import start_server  # noqa: E402

FIXTURES = TESTS / "fixtures"


@pytest.fixture(autouse=True, scope="session")
def _fastmcp_without_camelcase_compat():
    """MCP SDK v2 field names only. FastMCP's camelCase shim warns once per
    field per process, so a stale `inputSchema`-style read could slip past a
    warning filter; with the shim off it raises AttributeError every time."""
    try:
        import fastmcp
    except ImportError:
        yield
        return
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fastmcp.settings, "mcp_camelcase_compat", False)
        yield


@pytest.fixture(scope="session")
def calculator_wsdl() -> str:
    return str(FIXTURES / "calculator.wsdl")


@pytest.fixture(scope="session")
def orders_wsdl() -> str:
    return str(FIXTURES / "orders.wsdl")


@pytest.fixture(scope="session")
def advanced_wsdl() -> str:
    return str(FIXTURES / "advanced.wsdl")


@pytest.fixture(scope="session")
def soap_server():
    server, base_url = start_server()
    yield base_url
    server.shutdown()
