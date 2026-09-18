"""#155: operationId == FastMCP 4 tool name.

FastMCP 4 derives an OpenAPI tool's name from its operationId: the part
before the first '__', slugified, truncated to 56 characters, then made
unique with a '_2', '_3' suffix. Converted operationIds must already be in
that form, and verify() must catch documents where they are not.
"""
from __future__ import annotations

import pytest

from spec2openapi import check_fastmcp_ready, convert_swagger, verify
from spec2openapi.openapi import (
    FASTMCP_TOOL_NAME_MAX,
    _tool_id,
    _unique_id,
    fastmcp_tool_name,
)

LONG_A = "listPrivateLinkServicesAutoApprovedByResourceGroupAndRegion"       # 59
LONG_B = "listPrivateLinkServicesAutoApprovedByResourceGroupAndRegionPaged"  # 64


def _spec(ids):
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "servers": [{"url": "http://x.invalid"}],
            "paths": {f"/p{i}": {"get": {
                "operationId": oid, "summary": "s",
                "responses": {"200": {"description": "ok"}}}}
                for i, oid in enumerate(ids)}}


async def _exposed(spec):
    import httpx2
    from fastmcp import Client, FastMCP

    mcp = FastMCP.from_openapi(
        openapi_spec=spec, client=httpx2.AsyncClient(base_url="http://x"))
    async with Client(mcp) as client:
        return {t.name for t in await client.list_tools()}


# --- the model matches FastMCP 4 ---------------------------------------------

async def test_fastmcp_tool_name_matches_fastmcp():
    """If FastMCP changes its naming, this fails before users notice."""
    pytest.importorskip("fastmcp")
    ids = ["get__pets", "list-pets", "find.pet", "a---b", "trail__x",
           "_lead", "x" * 70, "Mixed_Case_1", "del ete", "q_é_r"]
    assert await _exposed(_spec(ids)) == {fastmcp_tool_name(i) for i in ids}


def test_the_limit_is_56():
    assert FASTMCP_TOOL_NAME_MAX == 56
    assert fastmcp_tool_name(LONG_B) == LONG_B[:56]


# --- generated ids are exposed unchanged -------------------------------------

@pytest.mark.parametrize("raw", [
    "Get__Thing", "a-.-b", "__x__", "x" * 90, "é_only", "op  name",
    "a" * 55 + "__b", "CamelCase123"])
def test_tool_id_is_its_own_tool_name(raw):
    tid = _tool_id(raw)
    assert fastmcp_tool_name(tid) == tid
    assert 0 < len(tid) <= FASTMCP_TOOL_NAME_MAX


def test_unique_id_suffix_stays_inside_the_limit_without_double_underscore():
    used: set[str] = set()
    base = "a" * 54 + "_b"                       # 56; truncating leaves '_'
    first = _unique_id(base, used)
    second = _unique_id(base, used)
    assert first == base
    assert len(second) <= FASTMCP_TOOL_NAME_MAX and "__" not in second
    assert fastmcp_tool_name(second) == second


# --- the reported document ----------------------------------------------------

LEGACY = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
          "host": "api.example.com", "paths": {
              "/a": {"get": {"operationId": LONG_A,
                             "responses": {"200": {"description": "ok"}}}},
              "/b": {"get": {"operationId": LONG_B,
                             "responses": {"200": {"description": "ok"}}}}}}


async def test_upgraded_long_ids_are_the_tool_names():
    pytest.importorskip("fastmcp")
    spec = convert_swagger(LEGACY)
    ids = [op["operationId"] for item in spec["paths"].values()
           for op in item.values()]
    assert all(len(i) <= FASTMCP_TOOL_NAME_MAX for i in ids)
    assert len(set(ids)) == 2
    assert await _exposed(spec) == set(ids)
    notes = spec["x-s2o"]["assumptions"]
    assert any(LONG_B in n for n in notes)


def test_upgraded_double_underscore_ids_are_the_tool_names():
    legacy = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
              "paths": {"/a": {"get": {"operationId": "pets__list",
                                       "responses": {"200": {
                                           "description": "ok"}}}}}}
    (oid,) = [op["operationId"] for item in convert_swagger(legacy)[
        "paths"].values() for op in item.values()]
    assert oid == "pets_list"


# --- verify() and check_fastmcp_ready() ---------------------------------------

def _statuses(report, cid):
    return [(r.status, r.message) for r in report.results if r.id == cid]


def test_an_operation_id_longer_than_56_fails_statically():
    spec = _spec([LONG_A])
    fails = _statuses(verify(spec, deep=False), "tool-name.length")
    assert fails and fails[0][0] == "fail"
    assert LONG_A[:56] in fails[0][1]
    assert any(LONG_A in p for p in check_fastmcp_ready(spec))


def test_ids_sharing_their_first_56_characters_collide():
    spec = _spec([LONG_A, LONG_A + "x"])
    (res,) = _statuses(verify(spec, deep=False),
                       "tool-name.normalization-collision")
    assert res[0] == "fail" and LONG_A[:56] in res[1]


def test_ids_sharing_the_part_before_a_double_underscore_collide():
    spec = _spec(["pets__v1", "pets__v2"])
    (res,) = _statuses(verify(spec, deep=False),
                       "tool-name.normalization-collision")
    assert res[0] == "fail" and "'pets'" in res[1]


def test_a_double_underscore_id_warns_with_its_exposed_name():
    spec = _spec(["pets__v1"])
    (res,) = _statuses(verify(spec, deep=False), "tool-name.normalized")
    assert res == ("warn", "operationId 'pets__v1' is exposed as tool "
                           "'pets' (FastMCP normalization)")


def test_materialized_names_are_compared_not_counted():
    pytest.importorskip("fastmcp")
    report = verify(_spec([LONG_A, LONG_A + "x"]))
    (res,) = _statuses(report, "fastmcp.tool-materialized")
    assert res[0] == "fail"
    assert LONG_A[:56] + "_2" in res[1]


def test_converted_long_ids_pass_verify():
    pytest.importorskip("fastmcp")
    report = verify(convert_swagger(LEGACY))
    assert report.ok, [r.message for r in report.results
                       if r.status == "fail"]


@pytest.mark.parametrize("oid", ["__internal", "-", ".", "_"])
def test_an_operation_id_with_no_exposed_tool_name_fails(oid):
    """FastMCP 4 keeps only the part before '__' and slugifies it - these
    ids leave an empty tool name."""
    assert fastmcp_tool_name(oid) == ""
    spec = _spec([oid])
    fails = [s for s in _statuses(verify(spec, deep=False),
                                  "tool-name.present") if s[0] == "fail"]
    assert fails and oid in fails[0][1]
    assert any(oid in p for p in check_fastmcp_ready(spec))


def test_the_rename_prompt_states_the_rule_apply_enforces():
    """apply 는 FastMCP 가 그대로 노출하지 않는 이름을 거부한다 - 프롬프트가
    같은 규칙을 말하지 않으면 모델의 개명이 버려진다."""
    import re

    from spec2openapi.agentize.prompts import _RENAME_RULES

    for right in re.findall(r"->\s+(\S+)", _RENAME_RULES):
        assert fastmcp_tool_name(right) == right, right
    rule = _RENAME_RULES[_RENAME_RULES.index("규칙:"):]
    for clause in ("[A-Za-z0-9_]", str(FASTMCP_TOOL_NAME_MAX), "연달아", "앞뒤"):
        assert clause in rule, clause


def test_length_is_judged_on_the_name_fastmcp_would_truncate():
    """FastMCP truncates the slug of the part before '__', not the raw id -
    a long id that leaves a short name is a rename, not a truncation."""
    oid = "getPets__" + "x" * 51                    # 60 chars -> 'getPets'
    report = verify(_spec([oid]), deep=False)
    assert not [s for s in _statuses(report, "tool-name.length")
                if s[0] == "fail"]
    assert _statuses(report, "tool-name.normalized") == [
        ("warn", f"operationId '{oid}' is exposed as tool 'getPets' "
                 "(FastMCP normalization)")]
