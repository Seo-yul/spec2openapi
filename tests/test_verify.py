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


def _mixed_dupe_operationid_spec():
    return _spec({
        "/a": {"get": {"operationId": 7, "responses": {}}},
        "/b": {"get": {"operationId": 7, "responses": {}}},
        "/c": {"get": {"operationId": "x", "responses": {}}},
        "/d": {"get": {"operationId": "x", "responses": {}}},
    })


def _xsoap_substitution_unhashable_member_spec():
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"Bad": {
        "x-soap-substitution": {
            "head": "payment", "namespace": "ns",
            "members": [{"element": ["not", "a", "string"]}],
        },
    }}}
    return spec


def _xsoap_choice_unhashable_member_spec():
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"C": {
        "type": "object", "properties": {"a": {}, "b": {}},
        "x-soap-choice": [["a", ["nested", "list"]]],
    }}}
    return spec


def test_verify_never_raises_on_garbage():
    for garbage in (None, [], {}, {"paths": None}, {"paths": {"/a": None}},
                    {"paths": {"/a": {"get": None}}},
                    {"paths": {"/a": {"get": {"operationId": 7,
                                              "responses": {}}}}},
                    _mixed_dupe_operationid_spec(),
                    _xsoap_substitution_unhashable_member_spec(),
                    _xsoap_choice_unhashable_member_spec()):
        report = verify(garbage, deep=False)
        assert isinstance(report, VerifyReport)


def test_check_fastmcp_ready_never_raises_on_mixed_type_duplicates():
    from spec2openapi import check_fastmcp_ready
    problems = check_fastmcp_ready(_mixed_dupe_operationid_spec())
    assert isinstance(problems, list)
    assert any("duplicate operationIds" in p for p in problems)


def _list_dupe_operationid_spec():
    # unhashable (list) operationIds: op_ids.count()/set-comprehension
    # over these previously crashed with `unhashable type: 'list'`.
    return _spec({
        "/a": {"get": {"operationId": ["x"], "responses": {}}},
        "/b": {"get": {"operationId": ["x"], "responses": {}}},
    })


def test_unique_check_handles_unhashable_operation_ids():
    spec = _list_dupe_operationid_spec()
    report = verify(spec, deep=False)
    assert isinstance(report, VerifyReport)
    (res,) = [r for r in report.results if r.id == "tool-name.unique"]
    assert res.status == "fail"
    assert "duplicate operationIds" in res.message

    from spec2openapi import check_fastmcp_ready
    problems = check_fastmcp_ready(spec)
    assert any("duplicate operationIds" in p for p in problems)


def test_unique_check_preserves_natural_sort_for_homogeneous_dupes():
    # finding #12: forcing key=repr on a duplicate set of same-typed,
    # naturally-orderable ids changed byte-identical output for input
    # that never crashed to begin with ({7, 10} -> [10, 7] via repr sort).
    spec = _spec({
        "/a": {"get": {"operationId": 7, "responses": {}}},
        "/b": {"get": {"operationId": 7, "responses": {}}},
        "/c": {"get": {"operationId": 10, "responses": {}}},
        "/d": {"get": {"operationId": 10, "responses": {}}},
    })
    report = verify(spec, deep=False)
    (res,) = [r for r in report.results if r.id == "tool-name.unique"]
    assert res.message == "duplicate operationIds: [7, 10]"


def test_has_operations_skips_when_no_paths():
    # previously an empty/missing `paths` made document.has-operations
    # return [] -> recorded as a dishonest "pass" (never actually
    # checked). It must report skip instead.
    report = verify({}, deep=False)
    (paths_res,) = [r for r in report.results if r.id == "document.has-paths"]
    assert paths_res.status == "fail"
    (ops_res,) = [r for r in report.results
                  if r.id == "document.has-operations"]
    assert ops_res.status == "skip"
    assert "not checked" in ops_res.message

    # fastmcp_ready_problems only collects fail-status messages, so the
    # frozen contract (document.has-operations never appearing there for
    # a paths-less spec) is unaffected by the skip.
    from spec2openapi import check_fastmcp_ready
    problems = check_fastmcp_ready({})
    assert not any("no operations" in p for p in problems)


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


def test_xsoap_version_missing_warns_invalid_fails():
    # an absent soapVersion falls back to the documented 1.1 default in
    # the bridge -> warn; an invalid value is always a defect -> fail.
    missing = _spec({"/op": _soap_op(xsoap={
        "endpoint": "http://e", "input": {"element": "In"},
    })})
    report = verify(missing, deep=False)
    (res,) = [r for r in report.results if r.id == "x-soap.version"]
    assert res.status == "warn"
    assert "soapVersion missing" in res.message

    invalid = _spec({"/op": _soap_op(xsoap={
        "soapVersion": "1,2", "endpoint": "http://e",
        "input": {"element": "In"},
    })})
    report = verify(invalid, deep=False)
    (res,) = [r for r in report.results if r.id == "x-soap.version"]
    assert res.status == "fail"


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


def test_xsoap_refs_scalar_headers_faults_no_crash():
    # headers/faults must normally be lists of entry dicts; a scalar there
    # (5, True) previously crashed via `xsoap.get(kind) or []` iterating
    # over a non-iterable/bool.
    spec = _spec({"/op": _soap_op(xsoap={
        "soapVersion": "1.1", "endpoint": "http://e",
        "input": {"element": "In"}, "headers": 5, "faults": True,
    })})
    report = verify(spec, deep=False)
    assert isinstance(report, VerifyReport)
    assert _statuses(report, "x-soap.refs") == [("pass", "")]


def test_xsoap_refs_skips_data_keyword_subtrees():
    # a $ref-shaped dict inside `example`/`examples`/`default`/`enum`/
    # `const` is data, not a schema reference, and must not be resolved.
    spec = _spec({"/op": _soap_op()})
    spec["paths"]["/op"]["post"]["responses"]["200"]["content"] = {
        "application/json": {
            "schema": {"type": "object"},
            "example": {"$ref": "#/components/schemas/JustData"},
        }}
    report = verify(spec, deep=False)
    assert _statuses(report, "x-soap.refs") == [("pass", "")]


def test_xsoap_refs_resolves_nested_pointer_first_segment():
    # `#/components/schemas/X/properties/b` points *into* X; only the
    # first segment (X) needs to exist in components.schemas.
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"X": {"type": "object",
                                            "properties": {"b": {}}}}}
    spec["paths"]["/op"]["post"]["responses"]["200"]["content"] = {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/X/properties/b"}}}
    report = verify(spec, deep=False)
    assert _statuses(report, "x-soap.refs") == [("pass", "")]

    spec["paths"]["/op"]["post"]["responses"]["200"]["content"] = {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/Missing/properties/b"}
        }}
    report = verify(spec, deep=False)
    fails = [m for s, m in _statuses(report, "x-soap.refs") if s == "fail"]
    assert any("Missing" in m for m in fails)


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
    # 실제 emitted 형태(schema.py ~317 / bridge.py ~256):
    # x-soap-choice는 {"members": [...], "required": bool} dict의 리스트.
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"C": {
        "type": "object", "properties": {"a": {}, "b": {}},
        "x-soap-choice": [{"members": ["a", "ghost"], "required": False}],
    }}}
    report = verify(spec, deep=False)
    (res,) = [(s, m) for s, m in _statuses(report, "x-soap.choice")]
    assert res[0] == "fail" and "ghost" in res[1]


def test_choice_marker_members_not_a_list_fails():
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"C": {
        "type": "object", "properties": {"a": {}, "b": {}},
        "x-soap-choice": [{"members": "a", "required": False}],
    }}}
    report = verify(spec, deep=False)
    (res,) = [(s, m) for s, m in _statuses(report, "x-soap.choice")]
    assert res[0] == "fail" and "members list" in res[1]


def test_choice_marker_group_not_a_dict_fails():
    spec = _spec({"/op": _soap_op()})
    spec["components"] = {"schemas": {"C": {
        "type": "object", "properties": {"a": {}, "b": {}},
        "x-soap-choice": [["a", "b"]],
    }}}
    report = verify(spec, deep=False)
    (res,) = [(s, m) for s, m in _statuses(report, "x-soap.choice")]
    assert res[0] == "fail"


def test_choice_marker_passes_on_real_world_example():
    # examples/advanced.openapi.yaml uses the real x-soap-choice marker
    # shape emitted by schema.py; the check must not false-positive on it.
    spec = yaml.safe_load(
        (EXAMPLES / "advanced.openapi.yaml").read_text(encoding="utf-8"))
    report = verify(spec, deep=False)
    assert all(s == "pass" for s, _ in _statuses(report, "x-soap.choice"))


def test_markers_pass_on_marker_free_spec():
    spec = _spec({"/op": _soap_op()})
    report = verify(spec, deep=False)
    assert _statuses(report, "x-soap.substitution") == [("pass", "")]
    assert _statuses(report, "x-soap.choice") == [("pass", "")]


def _deeply_nested_object_schema(depth):
    node = {"type": "string"}
    for _ in range(depth):
        node = {"type": "object", "properties": {"a": node}}
    return node


def test_deeply_nested_schema_does_not_recursion_error():
    # _iter_schema_nodes/_iter_ref_strings used to recurse in Python
    # call-stack frames; a ~4000-deep schema tree blew the recursion
    # limit. Both now walk with an explicit stack.
    spec = _spec({"/a": {"get": {"operationId": "a", "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})
    spec["components"] = {
        "schemas": {"Deep": _deeply_nested_object_schema(4000)}}
    report = verify(spec, deep=False)
    assert isinstance(report, VerifyReport)


def test_static_check_crash_becomes_fail_not_raise(monkeypatch):
    import spec2openapi.checks as checks_mod

    def _boom(spec):
        raise ValueError("boom")

    monkeypatch.setattr(checks_mod, "_STATIC_CHECKS",
                        [("document.has-paths", _boom)])
    spec = _spec({"/a": {"get": {"operationId": "a", "responses": {}}}})
    report = verify(spec, deep=False)
    (res,) = [r for r in report.results if r.id == "document.has-paths"]
    assert res.status == "fail"
    assert "check could not complete" in res.message
    assert "ValueError" in res.message
    assert "boom" in res.message


def test_fastmcp_ready_problems_crash_reported_not_raised(monkeypatch):
    import spec2openapi.checks as checks_mod

    def _boom(spec):
        raise ValueError("boom")

    monkeypatch.setattr(checks_mod, "_STATIC_CHECKS",
                        [("document.has-paths", _boom)])
    problems = checks_mod.fastmcp_ready_problems({"paths": {"/a": {}}})
    assert any("check could not complete" in p and "ValueError" in p
               for p in problems)


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


def test_tool_materialized_uses_count_invariant_not_name_prediction():
    # FastMCP's real tool-name normalization (version-specific: '__'
    # splitting, [\s.-] run collapsing, 64-char truncation in 3.4.7)
    # cannot be reliably predicted; the check must only compare counts.
    # `get_user__v2` materializes as tool "get_user" in fastmcp 3.4.7,
    # which the old name-prediction check flagged as missing.
    pytest.importorskip("fastmcp")
    pytest.importorskip("openapi_spec_validator")
    spec = _spec({"/a": {"get": {"operationId": "get_user__v2",
                                 "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})
    report = verify(spec)
    (mat,) = [r for r in report.results
              if r.id == "fastmcp.tool-materialized"]
    assert mat.status == "pass"


def test_roundtrip_skips_inside_running_event_loop():
    pytest.importorskip("fastmcp")
    import asyncio

    spec = _spec({"/a": {"get": {"operationId": "get_a", "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})

    async def _run():
        return verify(spec)

    report = asyncio.run(_run())
    (rt,) = [r for r in report.results if r.id == "fastmcp.roundtrip"]
    assert rt.status == "skip"
    assert "running event loop" in rt.message
    (mat,) = [r for r in report.results
              if r.id == "fastmcp.tool-materialized"]
    assert mat.status == "skip"
    assert report.ok is True


def test_tool_params_preserve_input_schema_order_not_alphabetized():
    # params order must mirror the OpenAPI/XSD-sequence declaration
    # order, not be alphabetized.
    pytest.importorskip("fastmcp")
    spec = _spec({"/a": {"get": {
        "operationId": "get_a", "summary": "s",
        "parameters": [
            {"name": "zeta", "in": "query", "required": True,
             "schema": {"type": "string"}},
            {"name": "alpha", "in": "query", "required": True,
             "schema": {"type": "string"}},
        ],
        "responses": {"200": {"description": "ok"}}}}})
    report = verify(spec)
    (rt,) = [r for r in report.results if r.id == "fastmcp.roundtrip"]
    assert rt.status == "pass"
    (tool,) = rt.data["tools"]
    assert tool["params"] == ["zeta", "alpha"]


def test_fastmcp_roundtrip_closes_dummy_client_on_success(monkeypatch):
    pytest.importorskip("fastmcp")
    import httpx as httpx_mod

    closed = []
    orig_aclose = httpx_mod.AsyncClient.aclose

    async def _tracking_aclose(self):
        closed.append(self)
        await orig_aclose(self)

    monkeypatch.setattr(httpx_mod.AsyncClient, "aclose", _tracking_aclose)

    spec = _spec({"/a": {"get": {"operationId": "get_a", "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})
    report = verify(spec)
    (rt,) = [r for r in report.results if r.id == "fastmcp.roundtrip"]
    assert rt.status == "pass"
    assert len(closed) == 1
    assert closed[0].is_closed


def test_fastmcp_roundtrip_closes_dummy_client_on_from_openapi_failure(
        monkeypatch):
    pytest.importorskip("fastmcp")
    import httpx as httpx_mod
    from fastmcp import FastMCP

    closed = []
    orig_aclose = httpx_mod.AsyncClient.aclose

    async def _tracking_aclose(self):
        closed.append(self)
        await orig_aclose(self)

    monkeypatch.setattr(httpx_mod.AsyncClient, "aclose", _tracking_aclose)

    def _boom(cls, *a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(FastMCP, "from_openapi", classmethod(_boom))

    spec = _spec({"/a": {"get": {"operationId": "get_a", "summary": "s",
                                 "responses": {"200": {"description": "ok"}}}}})
    report = verify(spec)
    (rt,) = [r for r in report.results if r.id == "fastmcp.roundtrip"]
    assert rt.status == "fail"
    assert len(closed) == 1
    assert closed[0].is_closed


def test_to_dict_data_dict_is_not_shared_with_source():
    data = {"tools": [{"name": "a", "params": []}]}
    res = CheckResult(id="fastmcp.roundtrip", status="pass", data=data)
    d = VerifyReport((res,)).to_dict()
    d["results"][0]["data"]["extra"] = "mutated"
    assert "extra" not in data
    assert "extra" not in res.data


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


from pathlib import Path

import yaml

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_public_api_exports():
    import spec2openapi
    for name in ("verify", "VerifyReport", "CheckResult", "CheckRef"):
        assert name in spec2openapi.__all__
        assert getattr(spec2openapi, name) is not None


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.openapi.yaml")),
                         ids=lambda p: p.name)
def test_examples_sweep_no_fails(example):
    spec = yaml.safe_load(example.read_text(encoding="utf-8"))
    report = verify(spec)
    fails = [r for r in report.results if r.status == "fail"]
    assert fails == [], [f"{r.id}@{r.location}: {r.message}" for r in fails]


def test_checks_module_does_not_import_optional_deps():
    # 새 프로세스에서 코어 import만으로 fastmcp/zeep가 로드되지 않아야 한다
    import subprocess
    code = ("import sys; import spec2openapi, spec2openapi.checks; "
            "bad = {'fastmcp', 'zeep', 'httpx'} & set(sys.modules); "
            "sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
