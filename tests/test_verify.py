"""tests/test_verify.py — verify()/checks.py의 구조화 검증 테스트."""
from __future__ import annotations

import sys
import pytest

from spec2openapi.checks import CheckRef, CheckResult, VerifyReport, verify


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


def _statuses(report, cid):
    return [(r.status, r.message) for r in report.results if r.id == cid]


def _soap_op(oid="op", xsoap=None, **extra):
    base = {"operationId": oid, "summary": "s",
            "x-soap": xsoap if xsoap is not None else {
                "soapVersion": "1.1", "endpoint": "http://e",
                "input": {"element": "In"}, "output": {"element": "Out"}},
            "responses": {"200": {"description": "ok"}}}
    base.update(extra)
    return {"post": base}


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


STATIC_IDS = {
    "document.has-paths", "document.has-operations",
    "tool-name.present", "tool-name.safe", "tool-name.unique",
    "tool-name.normalization-collision", "x-soap.input-element",
}
DEEP_IDS = {"openapi.schema-valid", "fastmcp.roundtrip",
            "fastmcp.tool-materialized"}


def test_verify_non_mapping_single_result():
    report = verify(None, deep=False)
    assert [r.id for r in report.results] == ["document.mapping"]
    assert report.results[0].status == "fail"
    assert report.ok is False


def test_verify_every_id_appears_once_when_clean():
    spec = _spec({"/a": {"get": {"operationId": "get_a", "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})
    report = verify(spec, deep=False)
    ids = [r.id for r in report.results]
    assert set(ids) >= STATIC_IDS
    assert set(ids) >= DEEP_IDS                      # skip으로라도 등장
    for cid in STATIC_IDS:
        (only,) = [r for r in report.results if r.id == cid]
        assert only.status == "pass"
    for cid in DEEP_IDS:
        (only,) = [r for r in report.results if r.id == cid]
        assert only.status == "skip"
        assert only.message == "deep=False"
    assert report.complete is False


def test_verify_findings_replace_pass_and_sort_deterministically():
    spec = _spec({
        "/b": {"get": {"operationId": "has space", "responses": {}}},
        "/a": {"get": {"operationId": "also bad", "responses": {}}},
    })
    r1 = verify(spec, deep=False)
    r2 = verify(spec, deep=False)
    assert r1 == r2                                   # 결정성
    fails = [r for r in r1.results if r.id == "tool-name.safe"]
    assert [f.location for f in fails] == sorted(f.location for f in fails)
    assert not any(r.id == "tool-name.safe" and r.status == "pass"
                   for r in r1.results)


def test_verify_never_raises_on_garbage():
    for garbage in (None, [], {}, {"paths": None}, {"paths": {"/a": None}},
                    {"paths": {"/a": {"get": None}}},
                    {"paths": {"/a": {"get": {"operationId": 7,
                                              "responses": {}}}}}):
        report = verify(garbage, deep=False)
        assert isinstance(report, VerifyReport)


def test_document_openapi3_check():
    swagger = {"swagger": "2.0", "info": {"title": "t", "version": "1"},
               "paths": {}}
    report = verify(swagger, deep=False)
    (res,) = [r for r in report.results if r.id == "document.openapi3"]
    assert res.status == "fail" and "convert_swagger" in res.message

    no_version = {"info": {"title": "t", "version": "1"}, "paths": {}}
    (res,) = [r for r in verify(no_version, deep=False).results
              if r.id == "document.openapi3"]
    assert res.status == "fail"


def test_tool_name_normalized_warns():
    spec = _spec({"/a": {"get": {"operationId": "get.a", "summary": "s",
                                 "responses": {}}}})
    (res,) = [r for r in verify(spec, deep=False).results
              if r.id == "tool-name.normalized"]
    assert res.status == "warn" and "get_a" in res.message


def test_tool_description_present_warns():
    spec = _spec({"/a": {"get": {"operationId": "a", "responses": {}}}})
    (res,) = [r for r in verify(spec, deep=False).results
              if r.id == "tool-description.present"]
    assert res.status == "warn"
    # description 또는 summary가 있으면 pass
    ok = _spec({"/a": {"get": {"operationId": "a", "description": "d",
                               "responses": {}}}})
    (res,) = [r for r in verify(ok, deep=False).results
              if r.id == "tool-description.present"]
    assert res.status == "pass"


def test_xsoap_version_output_endpoint_checks():
    spec = _spec({"/op": _soap_op(xsoap={
        "soapVersion": "1,2",              # 오타: fail
        "input": {"element": "In"},
        "output": {},                       # element 없음: warn
    })})                                    # endpoint 없음: warn
    report = verify(spec, deep=False)
    assert _statuses(report, "x-soap.version")[0][0] == "fail"
    assert _statuses(report, "x-soap.output-element")[0][0] == "warn"
    assert _statuses(report, "x-soap.endpoint")[0][0] == "warn"


def test_xsoap_checks_pass_on_pure_rest_spec():
    spec = _spec({"/a": {"get": {"operationId": "a", "summary": "s",
                                 "responses": {}}}})
    report = verify(spec, deep=False)
    for cid in ("x-soap.version", "x-soap.output-element", "x-soap.endpoint",
                "x-soap.mixed-rest", "x-soap.input-element"):
        assert _statuses(report, cid) == [("pass", "")], cid


def test_xsoap_mixed_rest_warns():
    paths = {"/op": _soap_op()}
    paths["/rest"] = {"get": {"operationId": "r", "summary": "s",
                              "responses": {}}}
    report = verify(_spec(paths), deep=False)
    assert _statuses(report, "x-soap.mixed-rest")[0][0] == "warn"


def test_xsoap_refs_detects_dangling():
    spec = _spec({"/op": _soap_op(xsoap={
        "soapVersion": "1.1", "endpoint": "http://e",
        "input": {"element": "In"},
        "headers": [{"element": "H", "part": "h",
                     "schema": "#/components/schemas/Missing"}],
    })})
    spec["paths"]["/op"]["post"]["responses"] = {
        "200": {"description": "ok", "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/AlsoMissing"}}}}}
    report = verify(spec, deep=False)
    msgs = [m for s, m in _statuses(report, "x-soap.refs") if s == "fail"]
    assert any("Missing" in m for m in msgs)
    assert any("AlsoMissing" in m for m in msgs)


def test_xsoap_refs_pass_when_resolvable():
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"X": {"type": "object"}}}
    spec["paths"]["/op"]["post"]["responses"]["200"]["content"] = {
        "application/json": {"schema": {"$ref": "#/components/schemas/X"}}}
    report = verify(spec, deep=False)
    assert _statuses(report, "x-soap.refs") == [("pass", "")]


def _sub_schema(members, branches):
    node = {"x-soap-substitution": {"head": "payment", "namespace": "ns",
                                    "members": members}}
    if branches is not None:
        node["oneOf"] = branches
    return node


def test_substitution_marker_violations():
    good_branch = {"type": "object",
                   "properties": {"creditCard": {"type": "object"}},
                   "required": ["creditCard"]}
    bad_branch = {"type": "object",
                  "properties": {"a": {}, "b": {}}, "required": ["a"]}
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {
        "NoElement": _sub_schema([{"namespace": "ns"}], None),
        "BadBranch": _sub_schema([{"element": "creditCard"}],
                                 [bad_branch]),
        "Mismatch": _sub_schema([{"element": "creditCard"}],
                                [{"type": "object",
                                  "properties": {"other": {}},
                                  "required": ["other"]}]),
        "Good": _sub_schema([{"element": "creditCard"}], [good_branch]),
        "AbstractHead": _sub_schema([], None),
    }}
    report = verify(spec, deep=False)
    fails = [m for s, m in _statuses(report, "x-soap.substitution")
             if s == "fail"]
    assert any("NoElement" in m or "element" in m for m in fails)
    assert any("single-property" in m for m in fails)
    assert any("do not match" in m for m in fails)
    assert len(fails) == 3          # Good/AbstractHead는 위반 아님


def test_choice_marker_violations():
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"C": {
        "type": "object", "properties": {"a": {}, "b": {}},
        "x-soap-choice": [["a", "ghost"]],
    }}}
    report = verify(spec, deep=False)
    (res,) = [(s, m) for s, m in _statuses(report, "x-soap.choice")]
    assert res[0] == "fail" and "ghost" in res[1]


def test_markers_pass_on_marker_free_spec():
    spec = _spec({"/op": _soap_op()})
    report = verify(spec, deep=False)
    assert _statuses(report, "x-soap.substitution") == [("pass", "")]
    assert _statuses(report, "x-soap.choice") == [("pass", "")]


def test_deep_checks_run_with_installed_deps():
    pytest.importorskip("fastmcp")
    pytest.importorskip("openapi_spec_validator")
    spec = _spec({"/a": {"get": {"operationId": "get_a", "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})
    report = verify(spec)                     # deep 기본값 True
    (osv,) = [r for r in report.results if r.id == "openapi.schema-valid"]
    assert osv.status == "pass"
    (rt,) = [r for r in report.results if r.id == "fastmcp.roundtrip"]
    assert rt.status == "pass"
    assert rt.data == {"tools": [{"name": "get_a", "params": []}]}
    (mat,) = [r for r in report.results
              if r.id == "fastmcp.tool-materialized"]
    assert mat.status == "pass"
    assert report.complete is True


def test_deep_checks_skip_when_fastmcp_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastmcp", None)   # import 실패 유도
    spec = _spec({"/a": {"get": {"operationId": "get_a", "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})
    report = verify(spec)
    (rt,) = [r for r in report.results if r.id == "fastmcp.roundtrip"]
    assert rt.status == "skip" and "spec2openapi[mcp]" in rt.message
    (mat,) = [r for r in report.results
              if r.id == "fastmcp.tool-materialized"]
    assert mat.status == "skip"
    assert report.complete is False
    assert report.ok is True                  # skip은 ok에 영향 없음


def test_deep_invalid_document_fails_schema_valid():
    pytest.importorskip("openapi_spec_validator")
    bad = {"openapi": "3.0.3", "paths": {}}   # info 없음: OAS 스키마 위반
    report = verify(bad)
    (osv,) = [r for r in report.results if r.id == "openapi.schema-valid"]
    assert osv.status == "fail"
