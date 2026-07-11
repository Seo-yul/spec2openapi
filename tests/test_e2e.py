"""End-to-end: WSDL -> spec -> FastMCP server -> MCP tool call -> mock SOAP
server -> typed JSON result back through the MCP client."""
from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from spec2openapi import BridgeOptions, convert_wsdl, from_openapi_spec, from_wsdl


def tool_data(result):
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    return json.loads(result.content[0].text)


@pytest.fixture()
def calc_mcp(calculator_wsdl, soap_server):
    opts = BridgeOptions(endpoint=f"{soap_server}/calc", trust_env=False)
    return from_wsdl(calculator_wsdl, options=opts)


@pytest.fixture()
def orders_mcp(orders_wsdl, soap_server):
    opts = BridgeOptions(endpoint=f"{soap_server}/orders", trust_env=False)
    spec = convert_wsdl(orders_wsdl)
    return from_openapi_spec(spec, options=opts)


async def test_list_tools(calc_mcp):
    async with Client(calc_mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"Add", "Divide"}
        assert "Adds two integers" in (tools["Add"].description or "")
        props = tools["Add"].inputSchema.get("properties", {})
        assert "a" in props and "b" in props


async def test_call_add(calc_mcp):
    async with Client(calc_mcp) as client:
        result = await client.call_tool("Add", {"a": 2, "b": 40})
        assert tool_data(result) == {"result": 42}


async def test_soap_fault_becomes_tool_error(calc_mcp):
    async with Client(calc_mcp) as client:
        with pytest.raises(ToolError, match="Division by zero"):
            await client.call_tool("Divide", {"numerator": 1, "denominator": 0})


async def test_create_order_nested(orders_mcp):
    async with Client(orders_mcp) as client:
        result = await client.call_tool(
            "CreateOrder",
            {
                "customer": {"name": "Alice", "email": "a@example.com"},
                "items": [
                    {"sku": "S1", "quantity": 2, "price": 19.99, "gift": True},
                    {"sku": "S2", "quantity": 1, "price": 5.5},
                ],
            },
        )
        data = tool_data(result)
        assert data["orderId"] == "ORD-2-G1"  # 2 items, 1 gift attribute seen
        assert data["status"] == "NEW"


async def test_get_order_arrays_and_types(orders_mcp):
    async with Client(orders_mcp) as client:
        result = await client.call_tool("GetOrder", {"orderId": "ORD-7"})
        data = tool_data(result)
        assert data["orderId"] == "ORD-7"
        assert data["customer"]["name"] == "Alice"
        assert len(data["items"]) == 2
        assert data["items"][0]["quantity"] == 2
        assert data["items"][0]["price"] == 19.99
        assert data["items"][0]["gift"] is True
        assert data["status"] == "SHIPPED"
