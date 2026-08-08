"""minify_for_mcp: the LLM-facing surface shrinks, serving never changes.

Covers the contract of #128: extension dropping scoped to schema
subtrees (components included), the keep lists, description truncation,
enrichment folds/hoisting with their pinned semantics, idempotency,
validity/readiness preservation, bridge-envelope invariance, and a
leak-map regression probe that fails loudly if a FastMCP upgrade changes
which spec locations reach tool payloads.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from spec2openapi import (
    ConversionError,
    check_fastmcp_ready,
    convert_swagger,
    convert_wsdl,
    load_spec,
    minify_for_mcp,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent / "examples"

ALL_ON = dict(max_description=200, enrich=("errors", "examples"))


def fixture_spec() -> dict:
    return load_spec(FIXTURES / "minify-input.openapi.yaml")


def create_pet(spec: dict) -> dict:
    return spec["paths"]["/pets"]["post"]


# --- extension dropping -----------------------------------------------------

def test_schema_extensions_dropped_operation_level_untouched():
    mini = minify_for_mcp(fixture_spec())
    op = create_pet(mini)
    assert "x-op-wiring" in op  # operation level never leaks; left alone
    body_schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert "x-root-wiring" not in body_schema
    assert "x-wire-hint" not in body_schema["properties"]["status"]
    # cleaning reaches $ref'd component schemas, not just inline ones
    assert "x-comp-wiring" not in mini["components"]["schemas"]["Trace"]


def test_doc_bearing_and_project_extensions_survive():
    mini = minify_for_mcp(fixture_spec())
    op = create_pet(mini)
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert props["status"]["x-enum-varnames"] == ["Active", "Deleted"]
    assert props["legacy"]["x-pattern"] == "(?i)legacy"
    resp_props = (op["responses"]["200"]["content"]["application/json"]
                  ["schema"]["properties"])
    assert "x-enum-descriptions" in resp_props["id"]


def test_dropped_extension_keys_recorded():
    mini = minify_for_mcp(fixture_spec())
    record = mini["x-s2o"]["minify"]
    assert set(record["droppedExtensionKeys"]) == {
        "x-root-wiring", "x-wire-hint", "x-comp-wiring",
    }
    assert record["counts"]["droppedExtensions"] == 3


def test_keep_extensions_exact_and_prefix():
    mini = minify_for_mcp(
        fixture_spec(), keep_extensions=("x-wire-hint", "x-root-*"))
    schema = create_pet(mini)["requestBody"]["content"]["application/json"]["schema"]
    assert schema["x-root-wiring"] == "dropped-at-root"  # prefix-kept
    assert "x-wire-hint" in schema["properties"]["status"]  # exact-kept
    assert "x-comp-wiring" not in mini["components"]["schemas"]["Trace"]


def test_xml_annotation_object_is_opaque():
    # xml -> x-text is written for xsd:simpleContent and read by the
    # bridge; a recursive x-* strip must not descend into `xml`
    spec = convert_wsdl(str(FIXTURES / "advanced.wsdl"))
    assert '"x-text": true' in json.dumps(spec["components"]["schemas"])
    mini = minify_for_mcp(spec, **ALL_ON)
    assert '"x-text": true' in json.dumps(mini["components"]["schemas"])


# --- no-op behavior ---------------------------------------------------------

def test_clean_soap_spec_is_a_noop(calculator_wsdl):
    spec = convert_wsdl(calculator_wsdl)
    mini = minify_for_mcp(spec)
    assert mini == spec          # nothing to remove -> equal document
    assert "x-s2o" not in mini   # and no record is invented


def test_input_not_mutated():
    spec = fixture_spec()
    snapshot = copy.deepcopy(spec)
    minify_for_mcp(spec, **ALL_ON)
    assert spec == snapshot


def test_idempotent_including_enrich_and_truncation():
    once = minify_for_mcp(fixture_spec(), **ALL_ON)
    twice = minify_for_mcp(once, **ALL_ON)
    assert twice == once


# --- drop_value_examples ----------------------------------------------------

def test_drop_value_examples_strips_generated_examples(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)  # #117 adds enum/date examples
    assert '"example"' in json.dumps(spec)
    mini = minify_for_mcp(spec, drop_value_examples=True)
    assert '"example"' not in json.dumps(mini)
    assert mini["x-s2o"]["minify"]["counts"]["droppedExamples"] > 0


# --- max_description --------------------------------------------------------

def test_max_description_truncates_only_leaking_descriptions():
    cap = 30
    mini = minify_for_mcp(fixture_spec(), max_description=cap)
    put_op = mini["paths"]["/pets/{petId}"]["put"]
    assert len(put_op["description"]) <= cap
    assert put_op["description"].endswith("…")
    shared = mini["components"]["parameters"]["SharedLimit"]
    assert len(shared["description"]) <= cap
    assert shared["description"].endswith("…")
    # summary never truncated (it does not leak when description exists)
    assert create_pet(mini)["summary"] == "Create a pet"
    # short descriptions untouched
    props = (create_pet(mini)["requestBody"]["content"]["application/json"]
             ["schema"]["properties"])
    assert props["name"]["description"] == "Display name of the pet."
    assert mini["x-s2o"]["minify"]["counts"]["truncatedDescriptions"] >= 2


# --- enrich: errors ---------------------------------------------------------

def test_errors_fold_wildcards_refs_and_order():
    mini = minify_for_mcp(fixture_spec(), enrich=("errors",))
    desc = create_pet(mini)["description"]
    assert desc.splitlines()[-1] == (
        "Errors: 404 (pet not found); 4XX (client error); "
        "default (unexpected error)."
    )
    assert "302" not in desc  # redirects are transport detail
    # folding copies facts; the responses themselves are untouched
    assert create_pet(mini)["responses"] == create_pet(fixture_spec())["responses"]


def test_errors_fold_preserves_summary_fallback():
    # FastMCP shows summary only while description is absent; a fold that
    # creates the description must start with the summary text
    mini = minify_for_mcp(fixture_spec(), enrich=("errors",))
    desc = mini["paths"]["/pets/{petId}"]["get"]["description"]
    assert desc == "Fetch one pet\nErrors: 404 (pet not found)."


def test_errors_fold_skips_structural_soap_fault(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)  # only the synthetic 500 exists
    mini = minify_for_mcp(spec, enrich=("errors",))
    assert mini == spec


# --- enrich: examples -------------------------------------------------------

def test_example_folds_both_shapes():
    mini = minify_for_mcp(fixture_spec(), enrich=("examples",))
    desc = create_pet(mini)["description"]
    assert 'Example request: {"name": "Bella", "status": "A"}' in desc
    # named Example Objects: externalValue-only entry skipped, first
    # entry with a value wins in document order
    assert 'Example response: {"id": 7, "name": "Bella"}' in desc


def test_example_fold_cap_and_json_media_only():
    mini = minify_for_mcp(fixture_spec(), enrich=("examples",))
    put_op = mini["paths"]["/pets/{petId}"]["put"]
    assert "Example request:" not in put_op["description"]
    notes = mini["x-s2o"]["minify"]["notes"]
    assert any("fold cap" in n for n in notes)
    assert not any("<pet/>" in n for n in notes)  # xml media never considered


def test_parameter_example_hoisting():
    mini = minify_for_mcp(fixture_spec(), enrich=("examples",))
    region, trace = create_pet(mini)["parameters"]
    assert region["schema"]["example"] == "eu-west-1"  # hoisted (moved)
    assert "example" not in region
    # a $ref schema is shared; hoisting into it would leak the example
    # into every referencing site — skipped and recorded
    assert trace["example"] == "tr-123"
    assert "example" not in mini["components"]["schemas"]["Trace"]
    assert any("parameter 'trace'" in n
               for n in mini["x-s2o"]["minify"]["notes"])
    shared = mini["components"]["parameters"]["SharedLimit"]
    assert shared["schema"]["example"] == 10
    assert "example" not in shared


def test_hoisting_yields_to_drop_value_examples():
    mini = minify_for_mcp(fixture_spec(), drop_value_examples=True,
                          enrich=("examples",))
    region = create_pet(mini)["parameters"][0]
    assert region["example"] == "eu-west-1"      # left in place
    assert "example" not in region["schema"]     # nothing added
    assert any("drop_value_examples" in n
               for n in mini["x-s2o"]["minify"]["notes"])


async def test_hoisted_example_reaches_the_tool_schema():
    mini = minify_for_mcp(fixture_spec(), enrich=("examples",))
    mcp = FastMCP.from_openapi(openapi_spec=mini, name="minify")
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    region = tools["createPet"].inputSchema["properties"]["region"]
    assert region.get("example") == "eu-west-1"


# --- input validation -------------------------------------------------------

def test_swagger2_input_is_redirected():
    legacy = load_spec(FIXTURES / "legacy-swagger.json")
    with pytest.raises(ConversionError, match="convert_swagger"):
        minify_for_mcp(legacy)


@pytest.mark.parametrize("bad", [None, [], "spec", {}, {"openapi": "4.0.0"}])
def test_non_openapi3_inputs_rejected(bad):
    with pytest.raises(ConversionError):
        minify_for_mcp(bad)


def test_unknown_enrich_category_is_loud():
    with pytest.raises(ConversionError, match="unknown enrich category"):
        minify_for_mcp(fixture_spec(), enrich=("error",))


def test_enrich_accepts_a_bare_string():
    mini = minify_for_mcp(fixture_spec(), enrich="errors")
    assert "Errors: " in create_pet(mini)["description"]


def test_max_description_must_be_positive():
    with pytest.raises(ConversionError, match="max_description"):
        minify_for_mcp(fixture_spec(), max_description=0)


# --- global contract --------------------------------------------------------

ALL_SPECS = sorted(EXAMPLES.glob("*.yaml"), key=lambda p: p.name)


@pytest.mark.parametrize("path", ALL_SPECS, ids=lambda p: p.stem)
def test_validity_and_readiness_preserved(path):
    spec = load_spec(path)
    assert check_fastmcp_ready(spec) == []
    mini = minify_for_mcp(spec, **ALL_ON)
    assert check_fastmcp_ready(mini) == []
    osv = pytest.importorskip("openapi_spec_validator")
    osv.validate(mini)


def test_swagger_pipeline_end_to_end():
    upgraded = convert_swagger(load_spec(FIXTURES / "legacy-swagger.json"))
    mini = minify_for_mcp(upgraded, **ALL_ON)
    assert check_fastmcp_ready(mini) == []


def test_properties_order_preserved(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)
    mini = minify_for_mcp(spec, **ALL_ON)
    for name, schema in spec["components"]["schemas"].items():
        if "properties" in schema:
            assert (list(mini["components"]["schemas"][name]["properties"])
                    == list(schema["properties"]))


def test_bridge_envelope_unchanged(orders_wsdl):
    from spec2openapi.bridge import BridgeOptions, _SpecIndex, build_envelope

    spec = convert_wsdl(orders_wsdl)
    mini = minify_for_mcp(spec, **ALL_ON)
    payload = {
        "customer": {"name": "Alice"},
        "items": [{"sku": "SKU-1", "quantity": 2, "price": 19.99,
                   "gift": True}],
        "note": "leave at door",
    }
    envelopes = []
    for doc in (spec, mini):
        index = _SpecIndex(doc)
        op = index.ops["/operations/CreateOrder"]
        envelopes.append(build_envelope(op, payload, index, BridgeOptions()))
    assert envelopes[0] == envelopes[1]


async def _tool_payload_sizes(spec: dict) -> dict[str, int]:
    mcp = FastMCP.from_openapi(openapi_spec=spec, name="size")
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return {
        t.name: len(json.dumps(
            {"description": t.description or "",
             "inputSchema": t.inputSchema,
             "outputSchema": getattr(t, "outputSchema", None)},
            ensure_ascii=False, sort_keys=True))
        for t in tools
    }


@pytest.mark.parametrize("path", ALL_SPECS, ids=lambda p: p.stem)
async def test_tool_payload_never_larger_with_defaults(path):
    spec = load_spec(path)
    before = await _tool_payload_sizes(spec)
    after = await _tool_payload_sizes(minify_for_mcp(spec))
    assert set(after) == set(before)
    for name in before:
        assert after[name] <= before[name]


# --- leak-map regression ----------------------------------------------------
# The options' premises rest on which spec locations FastMCP copies into
# tool payloads. If a FastMCP upgrade changes this map, fail loudly here
# instead of letting the premises rot silently.

_LEAK_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "leak-probe", "version": "1"},
    "servers": [{"url": "http://leak-probe.invalid"}],
    "paths": {"/orders": {"post": {
        "operationId": "createOrder",
        "summary": "SUMMARY_MARKER",
        "description": "DESCRIPTION_MARKER",
        "x-op-wiring": "OPWIRING_MARKER",
        "parameters": [{
            "name": "region", "in": "query",
            "description": "PARAMDESC_MARKER",
            "example": "PARAMEX_MARKER",
            "schema": {"type": "string"},
        }],
        "requestBody": {"content": {"application/json": {
            "example": {"REQEX_MARKER": 1},
            "schema": {
                "type": "object",
                "x-schema-wiring": "SCHEMAWIRING_MARKER",
                "properties": {"status": {
                    "type": "string",
                    "example": "VALEX_MARKER",
                    "description": "PROPDESC_MARKER",
                    "x-enum-varnames": ["ENUMVAR_MARKER"],
                }},
            },
        }}},
        "responses": {
            "200": {"description": "RESPDESC_MARKER"},
            "404": {"description": "ERR404_MARKER"},
        },
    }}},
}

_EXPECTED_LEAKS = {
    "DESCRIPTION_MARKER": True,
    "SUMMARY_MARKER": False,       # dropped whenever description exists
    "OPWIRING_MARKER": False,      # operation-level x-* never leaks
    "PARAMDESC_MARKER": True,      # merged into inputSchema
    "PARAMEX_MARKER": False,       # parameter-level example is dropped
    "REQEX_MARKER": False,         # media-type examples are dropped
    "SCHEMAWIRING_MARKER": False,  # input-schema root x-* is discarded
    "VALEX_MARKER": True,          # in-schema example survives
    "PROPDESC_MARKER": True,
    "ENUMVAR_MARKER": True,        # property-level x-* is copied verbatim
    "RESPDESC_MARKER": False,
    "ERR404_MARKER": False,        # error responses never leak
}


# --- review-hardening regressions (PR #129) ---------------------------------
# One test per confirmed finding of the PR review, in its severity order.

import datetime

import yaml


def _tiny(op_extra: dict | None = None, **root_extra) -> dict:
    op = {"operationId": "op1",
          "responses": {"200": {"description": "ok"}}}
    op.update(op_extra or {})
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/x": {"post": op}}}
    spec.update(root_extra)
    return spec


def test_non_dict_root_x_s2o_survives():
    spec = _tiny({"requestBody": {"content": {"application/json": {
        "schema": {"type": "object", "x-junk": 1}}}}})
    spec["x-s2o"] = "hand-written note"
    mini = minify_for_mcp(spec, enrich=("errors",))
    schema = (mini["paths"]["/x"]["post"]["requestBody"]["content"]
              ["application/json"]["schema"])
    assert "x-junk" not in schema           # dropping still works
    assert mini["x-s2o"] == "hand-written note"  # user data untouched
    assert minify_for_mcp(mini, enrich=("errors",)) == mini


def test_summary_promotion_truncates_on_first_run():
    spec = _tiny({"summary": "a summary well over the cap",
                  "responses": {"404": {"description": "missing"}}})
    once = minify_for_mcp(spec, max_description=10, enrich=("errors",))
    twice = minify_for_mcp(once, max_description=10, enrich=("errors",))
    assert twice == once
    desc = once["paths"]["/x"]["post"]["description"]
    base = desc.split("\n")[0]
    assert len(base) <= 10 and base.endswith("…")


def test_31_keywords_and_content_params_cleaned():
    spec = _tiny({
        "parameters": [{"name": "q", "in": "query", "content": {
            "application/json": {"schema": {"x-junk-param": 1}}}}],
        "requestBody": {"content": {"application/json": {"schema": {
            "$defs": {"Inner": {"x-junk-defs": 1}},
            "if": {"x-junk-if": 1},
            "then": {"x-junk-then": 1},
            "dependentSchemas": {"a": {"x-junk-dep": 1}},
            "unevaluatedProperties": {"x-junk-uneval": 1},
        }}}}})
    spec["openapi"] = "3.1.0"
    mini = minify_for_mcp(spec)
    assert "x-junk" not in json.dumps(mini["paths"])  # subtrees cleaned
    assert set(mini["x-s2o"]["minify"]["droppedExtensionKeys"]) == {
        "x-junk-param", "x-junk-defs", "x-junk-if", "x-junk-then",
        "x-junk-dep", "x-junk-uneval",
    }


def test_hoist_never_mutates_anchor_shared_schemas():
    spec = yaml.safe_load("""
openapi: 3.0.3
info: {title: t, version: "1"}
paths:
  /x:
    get:
      operationId: op1
      parameters:
        - {name: p1, in: query, example: FIRST, schema: &S {type: string}}
        - {name: p2, in: query, example: SECOND, schema: *S}
      responses:
        "200": {description: ok}
""")
    mini = minify_for_mcp(spec, enrich=("examples",))
    p1, p2 = mini["paths"]["/x"]["get"]["parameters"]
    assert p1["schema"]["example"] == "FIRST"
    assert p2["schema"]["example"] == "SECOND"


def test_cyclic_schema_does_not_recurse_forever():
    spec = yaml.safe_load("""
openapi: 3.0.3
info: {title: t, version: "1"}
paths:
  /x:
    get:
      operationId: op1
      responses: {"200": {description: ok}}
components:
  schemas:
    Node: &n
      type: object
      x-junk: 1
      properties:
        child: *n
""")
    mini = minify_for_mcp(spec)
    assert "x-junk" not in mini["components"]["schemas"]["Node"]
    assert mini["x-s2o"]["minify"]["counts"]["droppedExtensions"] == 1


def test_non_string_description_is_left_alone():
    spec = _tiny({"description": 123})
    assert minify_for_mcp(spec) == spec


def test_keep_extensions_accepts_a_bare_string():
    spec = _tiny({"requestBody": {"content": {"application/json": {
        "schema": {"x-acme-keep": 1, "x-junk": 2}}}}})
    mini = minify_for_mcp(spec, keep_extensions="x-acme-*")
    schema = (mini["paths"]["/x"]["post"]["requestBody"]["content"]
              ["application/json"]["schema"])
    assert schema == {"x-acme-keep": 1}


def test_enrich_accepts_a_generator():
    spec = _tiny({"responses": {"404": {"description": "nf"}}})
    mini = minify_for_mcp(spec, enrich=(c for c in ["errors"]))
    assert "Errors: 404 (nf)." in mini["paths"]["/x"]["post"]["description"]


def test_marker_prose_does_not_suppress_folds():
    spec = _tiny({"description": "Common Errors: are listed in the portal.",
                  "responses": {"404": {"description": "nf"}}})
    once = minify_for_mcp(spec, enrich=("errors",))
    desc = once["paths"]["/x"]["post"]["description"]
    assert desc == ("Common Errors: are listed in the portal.\n"
                    "Errors: 404 (nf).")
    assert minify_for_mcp(once, enrich=("errors",)) == once  # no dup


def test_error_fold_fragments_are_capped():
    spec = _tiny({"responses": {"500": {"description": "x" * 500}}})
    mini = minify_for_mcp(spec, enrich=("errors",))
    line = mini["paths"]["/x"]["post"]["description"].split("\n")[-1]
    assert line.startswith("Errors: 500 (") and len(line) < 100
    assert "…" in line


def test_summary_only_channel_is_capped():
    spec = _tiny({"summary": "a very long summary " * 5})
    del spec["paths"]["/x"]["post"]["responses"]  # keep it minimal
    spec["paths"]["/x"]["post"]["responses"] = {"200": {"description": "ok"}}
    mini = minify_for_mcp(spec, max_description=20)
    summary = mini["paths"]["/x"]["post"]["summary"]
    assert len(summary) <= 20 and summary.endswith("…")


@pytest.mark.parametrize("bad", [200.0, True, "50"])
def test_max_description_rejects_non_int(bad):
    with pytest.raises(ConversionError, match="max_description"):
        minify_for_mcp(_tiny(), max_description=bad)


def test_response_example_scan_continues_past_render_failures():
    spec = _tiny({"responses": {
        "200": {"description": "ok", "content": {"application/json": {
            "example": {"big": "x" * 400}}}},
        "201": {"description": "made", "content": {"application/json": {
            "example": {"small": 1}}}},
    }})
    mini = minify_for_mcp(spec, enrich=("examples",))
    desc = mini["paths"]["/x"]["post"]["description"]
    assert 'Example response: {"small": 1}' in desc


def test_nan_is_not_folded_and_dates_fold_as_isoformat():
    spec = _tiny({"requestBody": {"content": {"application/json": {
        "example": float("nan")}}}})
    mini = minify_for_mcp(spec, enrich=("examples",))
    # the source media example stays (folding never deletes), but no
    # "Example request: NaN" line may be written into the description
    assert "Example request:" not in json.dumps(
        mini["paths"]["/x"]["post"].get("description", ""))
    assert any("not JSON-serializable" in n
               for n in mini["x-s2o"]["minify"]["notes"])
    spec = _tiny({"requestBody": {"content": {"application/json": {
        "example": {"start": datetime.date(2024, 5, 1)}}}}})
    mini = minify_for_mcp(spec, enrich=("examples",))
    assert ('Example request: {"start": "2024-05-01"}'
            in mini["paths"]["/x"]["post"]["description"])


def test_marker_shaped_descriptions_survive_a_noop_run():
    spec = _tiny({"description": "\nErrors: handwritten note"})
    assert minify_for_mcp(spec) == spec


async def test_leak_map_regression():
    mcp = FastMCP.from_openapi(openapi_spec=_LEAK_SPEC, name="leak")
    async with Client(mcp) as client:
        tools = await client.list_tools()
    tool = tools[0]
    blob = json.dumps(
        {"description": tool.description, "inputSchema": tool.inputSchema,
         "outputSchema": getattr(tool, "outputSchema", None)},
        ensure_ascii=False)
    leaked = {m: (m in blob) for m in _EXPECTED_LEAKS}
    assert leaked == _EXPECTED_LEAKS
