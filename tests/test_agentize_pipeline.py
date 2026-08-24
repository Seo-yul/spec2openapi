"""agentize 파이프라인: FakeProvider로 전 구간을 결정론적으로 검증한다."""
from __future__ import annotations

import dataclasses

import pytest

from spec2openapi.agentize.providers import (
    GROUNDING,
    TRUSTED,
    FakeProvider,
    ProviderError,
    Suggestion,
)


def test_suggestion_is_frozen():
    s = Suggestion(pointer="#/a", description="d", grounding="named")
    assert s.example is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.description = "x"


def test_suggestion_is_hashable_with_scalar_fields():
    """Suggestion 은 필드가 해시 가능할 때만 해시 가능하다 - example 이
    dict 인 경우까지 약속하지는 않는다."""
    s = Suggestion(pointer="#/a", description="d", grounding="named")
    assert len({s, Suggestion(pointer="#/a", description="d",
                              grounding="named")}) == 1


def test_trusted_excludes_speculative():
    assert "speculative" not in TRUSTED
    assert {"named", "documented", "inferred"} == set(TRUSTED)


def test_grounding_grades_and_order():
    assert GROUNDING == ("named", "documented", "inferred", "speculative")


def test_fake_provider_returns_mapped_response_and_records_calls():
    fake = FakeProvider({"K1": {"fields": {"name": {
        "description": "표시용 이름", "grounding": "named"}}}})
    got = fake.complete("SYS", "K1", {})
    assert got["fields"]["name"]["grounding"] == "named"
    assert fake.calls == [("SYS", "K1")]


def test_fake_provider_raises_for_unmapped_key():
    fake = FakeProvider({})
    with pytest.raises(ProviderError):
        fake.complete("SYS", "unknown", {})


def test_fake_provider_accepts_callable():
    fake = FakeProvider(lambda user: {"echo": user})
    assert fake.complete("SYS", "hello", {}) == {"echo": "hello"}


# --- 오케스트레이션 ---------------------------------------------------------

import copy  # noqa: E402

from spec2openapi.agentize import (  # noqa: E402
    AgentizeError,
    agentize_spec,
)
from spec2openapi.agentize.providers import FakeProvider as FP  # noqa: E402


def pipeline_spec():
    return {
        "openapi": "3.0.3",
        "info": {"title": "PetStore", "version": "1"},
        "paths": {"/pets": {"post": {
            "operationId": "createPet",
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Pet"}}}},
            "responses": {"200": {"description": "ok"}}}}},
        "components": {"schemas": {"Pet": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "tag": {"type": "string"}}}}},
    }


def scripted_provider():
    """호출 종류를 user 문자열로 구분해 응답한다."""
    def respond(user):
        if '"glossary_request"' in user:
            return {"domain": "펫스토어", "overview": "반려동물 등록/조회",
                    "glossary": {"tag": "분류 태그"}}
        if '"schema": "Pet"' in user:
            return {"fields": {
                "name": {"description": "반려동물의 표시용 이름",
                         "grounding": "named"},
                "tag": {"description": "01=개 02=고양이",
                        "grounding": "speculative"}}}
        return {"summary": "반려동물을 새로 등록한다",
                "description": "이름과 태그로 반려동물 레코드를 만든다",
                "grounding": "inferred"}
    return FP(respond)


def test_pipeline_fills_schema_properties_and_drops_speculative():
    result = agentize_spec(pipeline_spec(), scripted_provider())
    props = result.spec["components"]["schemas"]["Pet"]["properties"]
    assert props["name"]["description"] == "반려동물의 표시용 이름"
    assert "description" not in props["tag"]
    assert result.report.dropped_speculative == 1


def test_pipeline_records_provenance():
    result = agentize_spec(pipeline_spec(), scripted_provider())
    block = result.spec["x-s2o"]["agentize"]
    assert block["provider"] == "fake"
    assert ("#/components/schemas/Pet/properties/name/description"
            in block["generated"])


def test_second_run_is_a_noop():
    once = agentize_spec(pipeline_spec(), scripted_provider())
    twice = agentize_spec(copy.deepcopy(once.spec), scripted_provider())
    assert twice.report.applied == {}
    assert twice.spec == once.spec


def test_schema_pass_runs_before_operation_pass():
    provider = scripted_provider()
    agentize_spec(pipeline_spec(), provider)
    users = [u for _, u in provider.calls]
    schema_i = next(i for i, u in enumerate(users) if '"schema": "Pet"' in u)
    op_i = next(i for i, u in enumerate(users) if '"operationId"' in u)
    assert schema_i < op_i


def test_shared_schema_is_asked_once_not_per_operation():
    spec = pipeline_spec()
    # 같은 스키마를 참조하는 operation을 하나 더 만든다
    spec["paths"]["/pets/{id}"] = {"put": {
        "operationId": "updatePet",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}],
        "requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Pet"}}}},
        "responses": {"200": {"description": "ok"}}}}
    provider = scripted_provider()
    agentize_spec(spec, provider)
    schema_calls = [u for _, u in provider.calls if '"schema": "Pet"' in u]
    assert len(schema_calls) == 1


def test_partial_failure_is_isolated_not_fatal():
    def respond(user):
        if '"glossary_request"' in user:
            return {"domain": "d", "overview": "o", "glossary": {}}
        if '"schema": "Pet"' in user:
            raise RuntimeError("모의 rate limit")
        return {"summary": "반려동물을 새로 등록한다",
                "description": "이름과 태그로 반려동물 레코드를 만든다",
                "grounding": "inferred"}
    result = agentize_spec(pipeline_spec(), FP(respond))
    assert result.failures and "Pet" in result.failures[0]
    # operation 패스는 계속 진행됐다
    assert result.spec["paths"]["/pets"]["post"]["description"]


def test_verify_failure_aborts_everything():
    """적용 결과가 verify를 깨면 산출물을 내지 않는다."""
    def respond(user):
        if '"glossary_request"' in user:
            return {"domain": "d", "overview": "o", "glossary": {}}
        if '"schema": "Pet"' in user:
            return {"fields": {}}
        # 두 operation에 같은 tool 이름을 주어 tool-name.unique를 깬다
        return {"operationId": "same_name", "summary": "s",
                "description": "이름과 태그로 레코드를 만든다",
                "grounding": "named"}
    spec = pipeline_spec()
    spec["paths"]["/pets/{id}"] = {"put": {
        "operationId": "updatePet",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}],
        "responses": {"200": {"description": "ok"}}}}
    with pytest.raises(AgentizeError, match="same_name"):
        agentize_spec(spec, FP(respond), rename_tools=True)


def test_non_mapping_x_s2o_refuses_to_run():
    spec = pipeline_spec()
    spec["x-s2o"] = "문자열"
    with pytest.raises(AgentizeError, match="x-s2o"):
        agentize_spec(spec, scripted_provider())


def test_max_ops_errors_before_any_call():
    provider = scripted_provider()
    with pytest.raises(AgentizeError, match="max-ops"):
        agentize_spec(pipeline_spec(), provider, max_ops=0)
    assert provider.calls == []


def test_pass_2a_ignores_fields_it_did_not_ask_for():
    """오케스트레이터는 자신이 요청한 필드만 받아들인다 - 이미 충실한
    설명이 덮이거나 재실행이 no-op 이 아니게 되는 것을 막는다."""
    spec = pipeline_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["name"][
        "description"] = "사람이 직접 쓴 반려동물 이름 설명"

    def respond(user):
        if '"glossary_request"' in user:
            return {"domain": "펫스토어", "overview": "개요", "glossary": {}}
        if '"schema": "Pet"' in user:
            return {"fields": {
                "name": {"description": "모델이 덮어쓰려 한 설명",
                         "grounding": "named"},
                "tag": {"description": "분류용 자유 태그", "grounding": "named"}}}
        return {"summary": "반려동물을 새로 등록한다",
                "description": "이름과 태그로 반려동물 레코드를 만든다",
                "grounding": "inferred"}

    result = agentize_spec(spec, FP(respond))
    props = result.spec["components"]["schemas"]["Pet"]["properties"]
    assert props["name"]["description"] == "사람이 직접 쓴 반려동물 이름 설명"
    assert props["tag"]["description"] == "분류용 자유 태그"
