"""XSD substitution groups -> oneOf of self-describing branches (#115)."""
from __future__ import annotations

import pytest
from lxml import etree

from spec2openapi import convert_wsdl
from spec2openapi.bridge import (
    BridgeOptions,
    _SpecIndex,
    build_envelope,
    parse_response,
)

validate = pytest.importorskip("openapi_spec_validator").validate

WSDL = "tests/fixtures/substitution.wsdl"
PAY_NS = "http://example.com/pay"


@pytest.fixture(scope="module")
def spec():
    return convert_wsdl(WSDL)


def _payment_prop(spec):
    return spec["paths"]["/operations/Pay"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]["properties"]["payment"]


def test_abstract_head_becomes_oneof_of_members(spec):
    pay = _payment_prop(spec)
    branch_keys = [b["required"][0] for b in pay["oneOf"]]
    # abstract head excluded; transitive member (visaCard) included
    assert sorted(branch_keys) == ["bankTransfer", "creditCard", "visaCard"]
    marker = pay["x-soap-substitution"]
    assert marker["head"] == "payment" and marker["namespace"] == PAY_NS
    assert {m["element"] for m in marker["members"]} == set(branch_keys)
    # every branch is a self-describing single-property object
    for b in pay["oneOf"]:
        assert list(b["properties"]) == b["required"]
        inner = b["properties"][b["required"][0]]
        assert inner["xml"]["namespace"] == PAY_NS


def test_concrete_head_is_its_own_branch(spec):
    notice = spec["paths"]["/operations/Pay"]["post"]["responses"]["200"][
        "content"]["application/json"]["schema"]["properties"]["notice"]
    branch_keys = sorted(b["required"][0] for b in notice["oneOf"])
    assert branch_keys == ["notice", "urgentNotice"]  # concrete head included


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_substitution_spec_validates(version):
    validate(convert_wsdl(WSDL, openapi_version=version))


def test_envelope_uses_member_element_name(spec):
    index = _SpecIndex(spec)
    op = index.ops["/operations/Pay"]
    env = build_envelope(
        op,
        {"orderId": "A1",
         "payment": {"creditCard": {"amount": 9.5, "cardNumber": "4111"}}},
        index, BridgeOptions(),
    )
    root = etree.fromstring(env)
    assert root.find(f".//{{{PAY_NS}}}creditCard") is not None
    assert root.find(f".//{{{PAY_NS}}}payment") is None  # head never on wire
    assert root.findtext(f".//{{{PAY_NS}}}cardNumber") == "4111"


@pytest.mark.parametrize("bad", [
    {"creditCard": {"amount": 1}, "bankTransfer": {"amount": 2}},  # two keys
    {"walletPay": {"amount": 1}},                                  # unknown
    "creditCard",                                                  # not a dict
])
def test_malformed_substitution_wrapper_rejected(spec, bad):
    index = _SpecIndex(spec)
    op = index.ops["/operations/Pay"]
    with pytest.raises(ValueError, match="payment"):
        build_envelope(op, {"orderId": "A1", "payment": bad},
                       index, BridgeOptions())


def test_response_wraps_wire_element_back(spec):
    index = _SpecIndex(spec)
    op = index.ops["/operations/Pay"]
    soap = (
        '<soapenv:Envelope xmlns:soapenv='
        '"http://schemas.xmlsoap.org/soap/envelope/">'
        '<soapenv:Body><PayResponse xmlns="http://example.com/pay">'
        "<ok>true</ok>"
        "<urgentNotice><text>pay now</text><deadline>friday</deadline>"
        "</urgentNotice>"
        "</PayResponse></soapenv:Body></soapenv:Envelope>"
    ).encode()
    status, data = parse_response(soap, op, index, BridgeOptions())
    assert status == 200
    assert data["ok"] is True
    assert data["notice"] == {
        "urgentNotice": {"text": "pay now", "deadline": "friday"}
    }


def test_fastmcp_roundtrip_keeps_branches(spec):
    pytest.importorskip("fastmcp")
    import anyio
    import httpx
    from fastmcp import Client, FastMCP

    mcp = FastMCP.from_openapi(
        openapi_spec=spec, name="subst",
        client=httpx.AsyncClient(base_url="http://x.invalid"),
    )

    async def _tools():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = anyio.run(_tools)
    assert [t.name for t in tools] == ["Pay"]
    arg = str(tools[0].inputSchema)
    # FastMCP normalizes oneOf to anyOf; the alternatives must survive
    assert "creditCard" in arg and "bankTransfer" in arg


async def test_e2e_tool_call_serializes_member_element(soap_server):
    """Full loop: MCP tool call -> bridge -> mock SOAP server -> wrapped
    response. The server faults if the abstract head appears on the wire."""
    pytest.importorskip("fastmcp")
    import json as _json

    from fastmcp import Client

    from spec2openapi import BridgeOptions, from_openapi_spec

    spec = convert_wsdl(WSDL)
    mcp = from_openapi_spec(
        spec, options=BridgeOptions(endpoint=f"{soap_server}/pay",
                                    trust_env=False),
    )
    async with Client(mcp) as client:
        result = await client.call_tool("Pay", {
            "orderId": "A1",
            "payment": {"creditCard": {"amount": 9.5, "cardNumber": "4111"}},
        })
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            data = getattr(result, "structured_content", None)
        if not isinstance(data, dict):
            data = _json.loads(result.content[0].text)
        assert data["ok"] is True
        assert data["notice"]["urgentNotice"]["text"] == \
            "paid 9.5 via creditCard"
