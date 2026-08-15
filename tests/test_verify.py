"""tests/test_verify.py — verify()/checks.py의 구조화 검증 테스트."""
from __future__ import annotations

import pytest

from spec2openapi.checks import CheckRef, CheckResult, VerifyReport


def test_report_ok_and_complete_flags():
    r_pass = CheckResult(id="a.b", status="pass")
    r_warn = CheckResult(id="a.c", status="warn", message="m")
    r_fail = CheckResult(id="a.d", status="fail", message="m")
    r_skip = CheckResult(id="a.e", status="skip", message="m")

    assert VerifyReport((r_pass, r_warn)).ok is True
    assert VerifyReport((r_pass, r_fail)).ok is False
    assert VerifyReport((r_pass, r_skip)).ok is True      # skip은 ok에 무관
    assert VerifyReport((r_pass, r_warn)).complete is True
    assert VerifyReport((r_pass, r_skip)).complete is False


def test_report_to_dict_shape():
    ref = CheckRef(id="SEP-986", url="https://example.invalid/sep986")
    res = CheckResult(id="tool-name.safe", status="fail", message="bad",
                      location="paths./x.get", refs=(ref,))
    d = VerifyReport((res,)).to_dict()
    assert d["ok"] is False
    assert d["complete"] is True
    assert d["summary"] == {"pass": 0, "warn": 0, "fail": 1, "skip": 0}
    assert d["results"] == [{
        "id": "tool-name.safe", "status": "fail", "message": "bad",
        "location": "paths./x.get",
        "refs": [{"id": "SEP-986", "url": "https://example.invalid/sep986"}],
        "data": None,
    }]
    import json
    json.dumps(d)  # JSON 직렬화 가능해야 한다


def test_results_are_immutable():
    res = CheckResult(id="a", status="pass")
    with pytest.raises(Exception):
        res.status = "fail"


from spec2openapi.checks import REGISTRY, _result

ALL_CHECK_IDS = {
    "document.mapping", "document.has-paths", "document.has-operations",
    "document.openapi3",
    "tool-name.present", "tool-name.safe", "tool-name.unique",
    "tool-name.normalization-collision", "tool-name.normalized",
    "tool-description.present",
    "x-soap.input-element", "x-soap.version", "x-soap.output-element",
    "x-soap.endpoint", "x-soap.refs", "x-soap.substitution",
    "x-soap.choice", "x-soap.mixed-rest",
    "openapi.schema-valid", "fastmcp.roundtrip", "fastmcp.tool-materialized",
}


def test_registry_covers_every_check_id():
    assert set(REGISTRY) == ALL_CHECK_IDS
    for cid, entry in REGISTRY.items():
        assert entry["statement"], cid
        assert entry["level"] in ("fail", "warn"), cid
        assert entry["refs"], cid            # 모든 체크에 근거 인용 존재


def test_result_helper_attaches_registry_refs():
    r = _result("tool-name.safe", "fail", "msg", location="paths./x.get")
    assert r.refs == REGISTRY["tool-name.safe"]["refs"]
    assert any(ref.id == "SEP-986" for ref in r.refs)


from spec2openapi.checks import fastmcp_ready_problems


def _spec(paths):
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": paths}


def test_ready_problems_matches_frozen_messages():
    spec = _spec({
        "/a": {"get": {"responses": {}}},                       # oid 없음
        "/b": {"get": {"operationId": "has space", "responses": {}}},
        "/c": {"get": {"operationId": "same", "responses": {}},
               "put": {"operationId": "same", "responses": {}}},
        "/d": {"get": {"operationId": "get.a", "responses": {}}},
        "/e": {"get": {"operationId": "get-a", "responses": {}}},
        "/f": {"post": {"operationId": "op", "x-soap": {"soapAction": ""},
                        "responses": {}}},
    })
    problems = fastmcp_ready_problems(spec)
    assert "GET /a: missing operationId" in problems
    assert "has space: not a safe MCP tool name" in problems
    assert "duplicate operationIds: ['same']" in problems
    assert ("operationIds collide after FastMCP normalization "
            "('get_a'): ['get-a', 'get.a']") in problems
    assert "op: x-soap.input.element missing" in problems


def test_ready_problems_frozen_edge_cases():
    assert fastmcp_ready_problems(None) == [
        "not an OpenAPI document (expected a mapping)"]
    assert "spec has no paths" in fastmcp_ready_problems({})
    assert "spec has no operations" in fastmcp_ready_problems(
        {"paths": {"/a": None}})
    # oid 없는 오퍼레이션은 x-soap/safe 검사 대상이 아니다 (원본의 continue)
    probs = fastmcp_ready_problems(_spec(
        {"/a": {"post": {"x-soap": {}, "responses": {}}}}))
    assert probs == ["POST /a: missing operationId"]
