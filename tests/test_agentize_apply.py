"""apply.py: 모델 출력이 스펙을 깨뜨릴 수 없음을 보장한다."""
from __future__ import annotations

import copy

from spec2openapi.agentize.apply import MAX_DESCRIPTION, apply_suggestions
from spec2openapi.agentize.providers import Suggestion


def base_spec():
    return {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {"/pets": {"post": {
            "operationId": "createPet",
            "summary": "반려동물을 등록한다",
            "x-soap": {"soapAction": "http://x/Create"},
            "parameters": [{"name": "trace", "in": "header",
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok"}}}}},
        "components": {"schemas": {"Pet": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"},
                           "tag": {"type": "string"}}}}},
    }


def test_description_is_written_at_a_valid_pointer():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "반려동물의 표시용 이름", "named")])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "description"] == "반려동물의 표시용 이름"
    assert rep.applied == {
        "#/components/schemas/Pet/properties/name/description": "named"}


def test_input_spec_is_not_mutated():
    spec = base_spec()
    before = copy.deepcopy(spec)
    apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "표시용 이름", "named")])
    assert spec == before


def test_speculative_is_dropped_by_default():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/tag/description",
        "01=강아지, 02=고양이", "speculative")])
    assert "description" not in out["components"]["schemas"]["Pet"][
        "properties"]["tag"]
    assert rep.dropped_speculative == 1


def test_speculative_is_kept_when_allowed():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/tag/description",
        "01=강아지, 02=고양이", "speculative")], allow_speculative=True)
    assert out["components"]["schemas"]["Pet"]["properties"]["tag"][
        "description"] == "01=강아지, 02=고양이"
    assert rep.dropped_speculative == 0


def test_unknown_grounding_is_rejected():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "x", "totally-sure")])
    assert out == spec
    assert rep.rejected and "grounding" in rep.rejected[0]


# --- 적대적 응답 방어 -------------------------------------------------------

def test_cannot_write_into_x_soap():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/x-soap/soapAction",
        "http://evil/Take", "named")])
    assert out["paths"]["/pets"]["post"]["x-soap"]["soapAction"] == \
        "http://x/Create"
    assert rep.rejected


def test_cannot_change_type():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/type", "integer", "named")])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "type"] == "string"


def test_cannot_change_required():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/required", "[]", "named")])
    assert out["components"]["schemas"]["Pet"]["required"] == ["name"]


def test_nonexistent_pointer_is_rejected_not_created():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Ghost/properties/x/description", "y", "named")])
    assert "Ghost" not in out["components"]["schemas"]
    assert rep.rejected


def test_overlong_description_is_truncated():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "가" * 100_000, "named")])
    got = out["components"]["schemas"]["Pet"]["properties"]["name"][
        "description"]
    assert len(got) <= MAX_DESCRIPTION


def test_non_string_description_is_rejected():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        {"nested": 1}, "named")])
    assert "description" not in out["components"]["schemas"]["Pet"][
        "properties"]["name"]
    assert rep.rejected


def test_operation_id_rename_requires_opt_in():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/operationId", "create_pet", "named")])
    assert out["paths"]["/pets"]["post"]["operationId"] == "createPet"
    assert rep.rejected

    out2, rep2 = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/operationId", "create_pet", "named")],
        rename_tools=True)
    assert out2["paths"]["/pets"]["post"]["operationId"] == "create_pet"
    assert rep2.renamed == {"#/paths/~1pets/post": "createPet"}


def test_parameter_description_uses_list_index():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/parameters/0/description",
        "요청 추적용 상관관계 ID", "named")])
    assert out["paths"]["/pets"]["post"]["parameters"][0]["description"] == \
        "요청 추적용 상관관계 ID"


def test_example_is_written_on_schema():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "표시용 이름", "named", example="바둑이")])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "example"] == "바둑이"


# --- example 상한 -----------------------------------------------------------

def test_oversized_example_is_rejected_not_truncated():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "반려동물의 표시용 이름", "named", example="가" * 10_000)])
    node = out["components"]["schemas"]["Pet"]["properties"]["name"]
    assert node["description"] == "반려동물의 표시용 이름"   # 설명은 적용된다
    assert "example" not in node                            # example 만 거부
    assert any("example" in r for r in rep.rejected)


def test_unserializable_example_is_rejected():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "반려동물의 표시용 이름", "named", example={1, 2, 3})])
    node = out["components"]["schemas"]["Pet"]["properties"]["name"]
    assert "example" not in node
    assert any("example" in r for r in rep.rejected)


def test_object_example_within_bounds_is_kept():
    """객체 example 은 정상이다 - 상한을 넘는 것만 막는다."""
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "반려동물의 표시용 이름", "named",
        example={"first": "바둑", "last": "이"})])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "example"] == {"first": "바둑", "last": "이"}
