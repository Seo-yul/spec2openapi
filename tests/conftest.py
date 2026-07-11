from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
sys.path.insert(0, str(TESTS))

from mock_soap_server import start_server  # noqa: E402

FIXTURES = TESTS / "fixtures"


@pytest.fixture(scope="session")
def calculator_wsdl() -> str:
    return str(FIXTURES / "calculator.wsdl")


@pytest.fixture(scope="session")
def orders_wsdl() -> str:
    return str(FIXTURES / "orders.wsdl")


@pytest.fixture(scope="session")
def soap_server():
    server, base_url = start_server()
    yield base_url
    server.shutdown()
