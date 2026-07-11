"""Robustness against messy real-world spec patterns: recursion, deep
nesting, large enums, name collisions, unqualified forms, duplicate
operation names across services, circular $refs, odd path characters."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from spec2openapi import (
    BridgeOptions,
    convert_swagger,
    convert_wsdl,
    from_openapi_spec,
    load_spec,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def stress_spec():
    return convert_wsdl(str(FIXTURES / "stress.wsdl"))


@pytest.fixture(scope="module")
def stress_s2o():
    return convert_swagger(load_spec(FIXTURES / "stress-swagger.json"))


# --------------------------------------------------------------- WSDL side


def test_recursive_type_becomes_self_ref(stress_spec):
    node = stress_spec["components"]["schemas"]["Node"]
    items = node["properties"]["children"]["items"]
    assert items["$ref"] == "#/components/schemas/Node"


def test_type_name_collision_across_namespaces(stress_spec):
    comps = stress_spec["components"]["schemas"]
    assert "Item" in comps and "Item_2" in comps
    fields = {tuple(comps["Item"]["properties"]),
              tuple(comps["Item_2"]["properties"])}
    assert fields == {("sku",), ("code",)}


def test_large_enum_preserved(stress_spec):
    classify = stress_spec["paths"]["/operations/Classify"]["post"]
    code = classify["requestBody"]["content"]["application/json"][
        "schema"]["properties"]["code"]
    assert len(code["enum"]) == 36
    assert "KR" in code["enum"] and "RU" in code["enum"]


def test_deep_nesting_chain(stress_spec):
    comps = stress_spec["components"]["schemas"]
    assert comps["L1"]["properties"]["l2"]["allOf"][0]["$ref"].endswith("/L2")
    assert comps["L2"]["properties"]["l3"]["allOf"][0]["$ref"].endswith("/L3")
    assert comps["L3"]["properties"]["l4"]["allOf"][0]["$ref"].endswith("/L4")
    assert comps["L4"]["properties"]["value"]["type"] == "string"


def test_duplicate_op_name_across_services(stress_spec):
    paths = set(stress_spec["paths"])
    assert "/operations/Ping" in paths
    assert "/operations/StressServiceB_Ping" in paths
    b = stress_spec["paths"]["/operations/StressServiceB_Ping"]["post"]
    assert b["x-soap"]["input"]["namespace"] == "http://example.com/stress2"


def test_unqualified_children_have_no_namespace(stress_spec):
    b = stress_spec["paths"]["/operations/StressServiceB_Ping"]["post"]
    schema = b["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["msg"]["xml"] == {"name": "msg"}  # no namespace


async def test_wsdl_stress_e2e(stress_spec, soap_server):
    opts = BridgeOptions(endpoint=f"{soap_server}/stress", trust_env=False)
    mcp = from_openapi_spec(stress_spec, options=opts)

    def data_of(result):
        if isinstance(result.data, dict):
            return result.data
        return json.loads(result.content[0].text)

    async with Client(mcp) as client:
        # recursive tree: depth 3, 5 nodes
        tree = {
            "name": "r",
            "children": [
                {"name": "a", "children": [{"name": "a1"}, {"name": "a2"}]},
                {"name": "b"},
            ],
        }
        res = data_of(await client.call_tool("EchoTree", {"root": tree}))
        assert res == {"depth": 3, "total": 5}

        # enum + collision type + 4-level nesting in one call
        res = data_of(await client.call_tool("Classify", {
            "code": "KR",
            "item": {"sku": "S-1"},
            "deep": {"l2": {"l3": {"l4": {"value": "deep"}}}},
        }))
        assert res == {"ok": True}

        # unqualified serialization round-trip (service B)
        res = data_of(await client.call_tool("StressServiceB_Ping", {
            "msg": "hello", "item": {"code": "Z9"},
        }))
        assert res == {"msg": "B:hello:Z9"}


# ------------------------------------------------------------ Swagger side


def test_circular_ref_survives_upgrade(stress_s2o):
    node = stress_s2o["components"]["schemas"]["Node"]
    assert node["properties"]["children"]["items"]["$ref"] == (
        "#/components/schemas/Node"
    )


def test_deep_allof_chain_rewritten(stress_s2o):
    c = stress_s2o["components"]["schemas"]["C"]
    assert c["allOf"][0]["$ref"] == "#/components/schemas/B"
    b = stress_s2o["components"]["schemas"]["B"]
    assert b["allOf"][0]["$ref"] == "#/components/schemas/A"


def test_odd_path_chars_make_safe_operation_ids(stress_s2o):
    ids = {
        op["operationId"]
        for item in stress_s2o["paths"].values()
        for op in item.values()
        if isinstance(op, dict)
    }
    # generated + explicit ids are normalized to FastMCP's tool-name alphabet
    assert "post_v1_items_item_id_activate" in ids
    assert "store_matrix" in ids  # was "store-matrix" in the source
    assert "get_health" in ids
    assert any("normalized to 'store_matrix'" in a
               for a in stress_s2o["x-s2o"]["assumptions"])


def test_nested_arrays_kept(stress_s2o):
    grid = stress_s2o["components"]["schemas"]["Matrix"]["properties"]["grid"]
    assert grid["items"]["items"]["type"] == "number"


def test_stress_swagger_spec_valid(stress_s2o):
    from openapi_spec_validator import validate as osv_validate

    osv_validate(stress_s2o)


async def test_stress_roundtrips_through_fastmcp(stress_spec, stress_s2o):
    for spec in (stress_spec, stress_s2o):
        op_ids = {
            op["operationId"]
            for item in spec["paths"].values()
            for op in item.values()
            if isinstance(op, dict)
        }
        mcp = FastMCP.from_openapi(openapi_spec=spec, name="stress")
        async with Client(mcp) as client:
            tools = {t.name for t in await client.list_tools()}
        assert tools == op_ids

    # recursive schema survives into the tool input schema
    mcp = FastMCP.from_openapi(openapi_spec=stress_spec, name="stress")
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    tree_schema = json.dumps(tools["EchoTree"].inputSchema)
    assert "Node" in tree_schema  # self-referencing definition present
