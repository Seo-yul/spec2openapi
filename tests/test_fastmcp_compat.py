"""The core guarantee of this project: every generated spec must convert
cleanly through FastMCP.from_openapi() into working MCP tools."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from spec2openapi import convert_wsdl
from spec2openapi.cli import main as cli_main

FIXTURES = Path(__file__).parent / "fixtures"
ALL_WSDLS = sorted(FIXTURES.glob("*.wsdl"))

TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@pytest.mark.parametrize("wsdl", ALL_WSDLS, ids=lambda p: p.stem)
@pytest.mark.parametrize("openapi_version", ["3.0", "3.1"])
async def test_fastmcp_roundtrip_all_fixtures(wsdl, openapi_version):
    spec = convert_wsdl(str(wsdl), openapi_version=openapi_version)
    op_ids = {
        op["operationId"]
        for item in spec["paths"].values()
        for op in item.values()
    }

    mcp = FastMCP.from_openapi(openapi_spec=spec, name="compat")
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    # every operation materializes as a tool with a safe name
    assert set(tools) == op_ids
    for name, tool in tools.items():
        assert TOOL_NAME_RE.fullmatch(name)
        schema = tool.inputSchema or {}
        assert schema.get("type", "object") == "object"


async def test_tool_schema_content_matches_wsdl():
    spec = convert_wsdl(str(FIXTURES / "advanced.wsdl"))
    mcp = FastMCP.from_openapi(openapi_spec=spec, name="compat")
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    tool = tools["SubmitApplication"]

    props = tool.inputSchema["properties"]
    assert set(props) >= {"applicant", "payment", "discount", "email",
                          "phone", "tags", "mode"}
    # facet + doc survive into the tool schema FastMCP hands to the LLM
    assert props["discount"]["maximum"] == 100.0
    assert tool.description and "Submits an application" in tool.description


def test_validate_cli_ok(tmp_path, capsys):
    spec_path = tmp_path / "adv.yaml"
    rc = cli_main(["convert", str(FIXTURES / "advanced.wsdl"),
                   "-o", str(spec_path)])
    assert rc == 0
    rc = cli_main(["validate", str(spec_path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "FastMCP round-trip: OK" in out
    assert "spec is FastMCP-convertible" in out


def test_validate_cli_catches_broken_spec(tmp_path, capsys):
    import yaml

    spec = convert_wsdl(str(FIXTURES / "calculator.wsdl"))
    for item in spec["paths"].values():
        item["post"].pop("operationId")  # break it
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    rc = cli_main(["validate", str(bad)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "missing operationId" in out
