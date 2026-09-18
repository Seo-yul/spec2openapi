"""apply.py: 모델 출력이 스펙을 깨뜨릴 수 없음을 보장한다."""
from __future__ import annotations

import copy

import pytest

from spec2openapi.agentize.apply import (
    MAX_DESCRIPTION,
    already_generated,
    apply_suggestions,
    can_record,
    record_provenance,
)
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
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/type", "integer", "named")])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "type"] == "string"
    assert rep.rejected


def test_cannot_change_required():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/required", "[]", "named")])
    assert out["components"]["schemas"]["Pet"]["required"] == ["name"]
    assert rep.rejected


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


# --- 리뷰 발견: 후행 개행, /example 포인터, operationId 안전성 ---------------

def test_trailing_newline_pointer_is_rejected():
    """`$` 는 끝 직전 개행에도 매치된다 - 화이트리스트가 승인하지 않은
    'description\\n' 키가 쓰이면 안 된다."""
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description\n",
        "침투 시도입니다", "named")])
    assert list(out["components"]["schemas"]["Pet"]["properties"]["name"]) == [
        "type"]
    assert rep.applied == {} and rep.rejected


def test_example_pointer_is_bounded():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/example",
        "가" * 10_000, "named")])
    assert "example" not in out["components"]["schemas"]["Pet"]["properties"][
        "name"]
    assert any("example" in r for r in rep.rejected)


def test_example_pointer_within_bounds_is_written():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/example", "바둑이", "named")])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "example"] == "바둑이"


def test_example_is_not_written_onto_an_operation():
    """Operation Object 에는 example 필드가 없다."""
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/description", "반려동물을 등록한다", "named",
        example="예시")])
    assert "example" not in out["paths"]["/pets"]["post"]


def test_unsafe_operation_id_is_rejected():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/operationId", "x" * 5000, "named")],
        rename_tools=True)
    assert out["paths"]["/pets"]["post"]["operationId"] == "createPet"
    assert rep.rejected

    out2, rep2 = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/operationId", "create pet\n", "named")],
        rename_tools=True)
    assert out2["paths"]["/pets"]["post"]["operationId"] == "createPet"
    assert rep2.rejected


def test_creating_an_operation_id_is_not_recorded_as_a_rename():
    spec = base_spec()
    spec["paths"]["/pets"]["put"] = {"responses": {"200": {"description": "ok"}}}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/put/operationId", "update_pet", "named")],
        rename_tools=True)
    assert out["paths"]["/pets"]["put"]["operationId"] == "update_pet"
    assert rep.renamed == {}
    assert "#/paths/~1pets/put/operationId" in rep.applied


def test_rejected_message_bounds_and_escapes_the_pointer():
    spec = base_spec()
    nasty = "#/nope/\x1b[2J\r" + "x" * 5000
    _, rep = apply_suggestions(spec, [Suggestion(nasty, "설명입니다", "named")])
    assert rep.rejected
    entry = rep.rejected[0]
    assert len(entry) < 400
    assert "\x1b" not in entry and "\r" not in entry


def test_control_characters_are_stripped_from_descriptions():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "정상\x1b[2J텍스트\r입니다", "named")])
    got = out["components"]["schemas"]["Pet"]["properties"]["name"][
        "description"]
    assert got == "정상텍스트입니다"


def test_out_of_whitelist_pointer_is_reported_even_when_speculative():
    """프롬프트 인젝션 신호는 등급과 무관하게 보존되어야 한다."""
    spec = base_spec()
    _, rep = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/x-soap/soapAction", "http://evil", "speculative")])
    assert rep.rejected and rep.dropped_speculative == 0


def test_bracketed_text_is_not_mistaken_for_an_escape_sequence():
    """제어문자 제거는 리터럴 ESC 접두를 요구한다 - 대괄호만으로는
    정상 텍스트가 잘리지 않는다."""
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "array[2] 형식의 [0-9;]* 패턴을 쓴다", "named")])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "description"] == "array[2] 형식의 [0-9;]* 패턴을 쓴다"


def test_example_field_is_cleaned_like_a_description():
    """같은 문자열이 example= 필드로 오든 /example 포인터로 오든
    동일하게 정리되어야 한다."""
    spec = base_spec()
    dirty = "\x1b[31m빨강\x1b[0m 텍스트\r"
    by_field, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "반려동물의 표시용 이름", "named", example=dirty)])
    by_pointer, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/example", dirty, "named")])
    got = by_field["components"]["schemas"]["Pet"]["properties"]["name"][
        "example"]
    assert got == "빨강 텍스트"
    assert got == by_pointer["components"]["schemas"]["Pet"]["properties"][
        "name"]["example"]


def test_nested_example_structures_are_cleaned_recursively():
    spec = base_spec()
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "반려동물의 표시용 이름", "named",
        example={"이름": "\x1b[2J바둑이", "목록": ["가\r나"]})])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "example"] == {"이름": "바둑이", "목록": ["가나"]}


# --- provenance -------------------------------------------------------------

def test_provenance_lives_only_at_root():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "표시용 이름", "named")])
    record_provenance(out, rep, provider="fake", model="fake-1",
                      targets=("properties",))
    block = out["x-s2o"]["agentize"]
    assert block["provider"] == "fake" and block["model"] == "fake-1"
    assert block["generated"] == {
        "#/components/schemas/Pet/properties/name/description":
            {"grounding": "named", "provider": "fake", "model": "fake-1"}}
    # 스키마 노드에는 어떤 키도 추가되지 않는다
    node = out["components"]["schemas"]["Pet"]["properties"]["name"]
    assert set(node) == {"type", "description"}


def test_already_generated_reads_the_record_back():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "표시용 이름", "named")])
    record_provenance(out, rep, provider="fake", model="fake-1",
                      targets=("properties",))
    assert already_generated(out) == {
        "#/components/schemas/Pet/properties/name/description"}
    assert already_generated(base_spec()) == set()


def test_can_record_rejects_non_mapping_x_s2o():
    spec = base_spec()
    assert can_record(spec) is True
    spec["x-s2o"] = "무언가 다른 값"
    assert can_record(spec) is False


def test_record_preserves_existing_x_s2o_keys():
    spec = base_spec()
    spec["x-s2o"] = {"source": "swagger-2.0", "assumptions": ["a"]}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "표시용 이름", "named")])
    record_provenance(out, rep, provider="fake", model="fake-1",
                      targets=("properties",))
    assert out["x-s2o"]["source"] == "swagger-2.0"
    assert out["x-s2o"]["assumptions"] == ["a"]
    assert "agentize" in out["x-s2o"]


def test_rename_is_recorded():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/paths/~1pets/post/operationId", "create_pet", "named")],
        rename_tools=True)
    record_provenance(out, rep, provider="fake", model="fake-1",
                      targets=("desc",))
    assert out["x-s2o"]["agentize"]["renamed"] == {
        "#/paths/~1pets/post": {"from": "createPet"}}


def test_dropped_speculative_reflects_only_the_current_run():
    """이전 실행의 누적이 아니라 이번 실행의 값이어야 한다 (Ruling 60) -
    누적이면 재실행할 때마다 실제와 어긋난 수가 영구히 남는다."""
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/tag/description",
        "01=강아지, 02=고양이", "speculative")])
    record_provenance(out, rep, provider="fake", model="fake-1",
                      targets=("properties",))
    assert out["x-s2o"]["agentize"]["dropped_speculative"] == 1

    # 두 번째 실행에서 아무것도 폐기하지 않았다면 기록도 0 이어야 한다 -
    # 첫 실행의 1 이 남아 있으면 안 된다.
    out2, rep2 = apply_suggestions(out, [], allow_speculative=True)
    record_provenance(out2, rep2, provider="fake", model="fake-1",
                      targets=("properties",))
    assert out2["x-s2o"]["agentize"]["dropped_speculative"] == 0


def test_generated_entries_keep_their_own_provider_and_model():
    """서로 다른 provider/model 로 두 번 실행하면 각 포인터는 자신을 쓴
    provider/model 을 유지해야 한다 - 최상위 필드는 마지막 실행 값이지만
    개별 포인터가 last-write-wins 로 덮이면 어느 문장을 어느 모델이
    썼는지 사후에 구분할 수 없다 (Ruling 64)."""
    spec = base_spec()
    out1, rep1 = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "표시용 이름", "named")])
    record_provenance(out1, rep1, provider="anthropic", model="claude-opus-5",
                      targets=("properties",))

    out2, rep2 = apply_suggestions(out1, [Suggestion(
        "#/components/schemas/Pet/properties/tag/description",
        "분류용 태그", "named")])
    record_provenance(out2, rep2, provider="openai", model="gpt-5",
                      targets=("properties",))

    block = out2["x-s2o"]["agentize"]
    assert block["provider"] == "openai" and block["model"] == "gpt-5"
    name_entry = block["generated"][
        "#/components/schemas/Pet/properties/name/description"]
    tag_entry = block["generated"][
        "#/components/schemas/Pet/properties/tag/description"]
    assert name_entry["provider"] == "anthropic"
    assert name_entry["model"] == "claude-opus-5"
    assert tag_entry["provider"] == "openai"
    assert tag_entry["model"] == "gpt-5"


# --- example type 모순 방어 (Ruling 53) --------------------------------------

def test_string_example_on_integer_property_is_rejected():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["qty"] = {
        "type": "integer"}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/qty/description",
        "재고 수량", "named", example="twelve")])
    node = out["components"]["schemas"]["Pet"]["properties"]["qty"]
    assert "example" not in node
    assert any("example" in r for r in rep.rejected)


def test_numeric_string_example_on_integer_property_is_parsed_and_written():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["qty"] = {
        "type": "integer"}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/qty/description",
        "재고 수량", "named", example="12")])
    node = out["components"]["schemas"]["Pet"]["properties"]["qty"]
    assert node["example"] == 12
    assert isinstance(node["example"], int) and not isinstance(
        node["example"], bool)
    assert rep.rejected == []


def test_string_example_on_boolean_property_is_rejected():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["active"] = {
        "type": "boolean"}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/active/description",
        "판매 가능 여부", "named", example="yes please")])
    node = out["components"]["schemas"]["Pet"]["properties"]["active"]
    assert "example" not in node
    assert any("example" in r for r in rep.rejected)


def test_boolean_string_example_on_boolean_property_is_parsed():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["active"] = {
        "type": "boolean"}
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/active/description",
        "판매 가능 여부", "named", example="true")])
    assert out["components"]["schemas"]["Pet"]["properties"]["active"][
        "example"] is True


def test_string_example_on_string_property_is_unaffected():
    spec = base_spec()
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/name/description",
        "표시용 이름", "named", example="바둑이")])
    assert out["components"]["schemas"]["Pet"]["properties"]["name"][
        "example"] == "바둑이"
    assert rep.rejected == []


def test_example_on_property_without_a_declared_type_is_unaffected():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["misc"] = {}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/misc/description",
        "기타 필드", "named", example="아무값")])
    assert out["components"]["schemas"]["Pet"]["properties"]["misc"][
        "example"] == "아무값"
    assert rep.rejected == []


def test_string_example_on_array_property_is_rejected():
    """모델의 example 은 언제나 문자열이다. 배열 필드에 "[1, 2]" 같은
    문자열이 붙으면 FastMCP 가 그대로 payload 에 실어 agent 를 오도한다."""
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["ids"] = {
        "type": "array", "items": {"type": "integer"}}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/ids/description",
        "연결된 ID 목록", "named", example="1, 2")])
    assert "example" not in out["components"]["schemas"]["Pet"][
        "properties"]["ids"]
    assert any("example" in r for r in rep.rejected)


def test_json_array_example_on_array_property_is_parsed_and_written():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["ids"] = {
        "type": "array", "items": {"type": "integer"}}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/ids/description",
        "연결된 ID 목록", "named", example="[1, 2]")])
    assert out["components"]["schemas"]["Pet"]["properties"]["ids"][
        "example"] == [1, 2]
    assert rep.rejected == []


def test_json_example_of_the_wrong_shape_on_object_property_is_rejected():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["meta"] = {
        "type": "object"}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/meta/example",
        "[1, 2]", "named")])
    assert "example" not in out["components"]["schemas"]["Pet"][
        "properties"]["meta"]
    assert any("example" in r for r in rep.rejected)


def test_json_object_example_on_object_property_is_parsed_and_written():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["meta"] = {
        "type": "object"}
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/meta/example",
        '{"color": "brown"}', "named")])
    assert out["components"]["schemas"]["Pet"]["properties"]["meta"][
        "example"] == {"color": "brown"}


@pytest.mark.parametrize("raw", ["[NaN, 1]", "[Infinity]", "[1e999]",
                                 '{"x": -Infinity}'])
def test_non_finite_json_example_on_structured_property_is_rejected(raw):
    spec = base_spec()
    props = spec["components"]["schemas"]["Pet"]["properties"]
    props["ids"] = {"type": "array"}
    props["meta"] = {"type": "object"}
    target = "meta" if raw.startswith("{") else "ids"
    out, rep = apply_suggestions(spec, [Suggestion(
        f"#/components/schemas/Pet/properties/{target}/example", raw,
        "named")])
    assert "example" not in out["components"]["schemas"]["Pet"][
        "properties"][target]
    assert any("example" in r for r in rep.rejected)


def test_deeply_nested_json_example_is_rejected_not_raised():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["ids"] = {
        "type": "array"}
    raw = "[" * 100_000 + "]" * 100_000
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/ids/example", raw, "named")])
    assert "example" not in out["components"]["schemas"]["Pet"][
        "properties"]["ids"]
    assert any("example" in r for r in rep.rejected)


def _two_folded_ops():
    spec = base_spec()
    spec["paths"] = {
        "/a": {"get": {"operationId": "a",
                       "description": "Errors: 404 (A missing).",
                       "responses": {"200": {"description": "ok"}}}},
        "/b": {"get": {"operationId": "b",
                       "description": "Errors: 409 (B conflict).",
                       "responses": {"200": {"description": "ok"}}}}}
    spec["x-s2o"] = {"minify": {"folded": {"a": ["errors"],
                                           "b": ["errors", "examples"]}}}
    return spec


def test_chained_renames_keep_each_fold_record_with_its_operation():
    """a->b, b->c 를 한 번에 적용해도 각 기록은 자기 operation 을 따라간다."""
    out, _ = apply_suggestions(_two_folded_ops(), [
        Suggestion("#/paths/~1a/get/description", "A 를 조회한다", "named"),
        Suggestion("#/paths/~1a/get/operationId", "b", "named"),
        Suggestion("#/paths/~1b/get/description", "B 를 조회한다", "named"),
        Suggestion("#/paths/~1b/get/operationId", "c", "named"),
    ], rename_tools=True)
    assert out["x-s2o"]["minify"]["folded"] == {
        "b": ["errors"], "c": ["errors", "examples"]}
    assert out["paths"]["/a"]["get"]["description"] == (
        "A 를 조회한다\nErrors: 404 (A missing).")
    assert out["paths"]["/b"]["get"]["description"] == (
        "B 를 조회한다\nErrors: 409 (B conflict).")


def test_a_description_of_only_fold_lines_is_rejected():
    out, rep = apply_suggestions(_two_folded_ops(), [Suggestion(
        "#/paths/~1a/get/description", "Errors: 404 (A missing).", "named")])
    assert out["paths"]["/a"]["get"]["description"] == (
        "Errors: 404 (A missing).")
    assert "#/paths/~1a/get/description" not in rep.applied
    assert rep.rejected


def test_swapped_names_swap_their_fold_records():
    out, _ = apply_suggestions(_two_folded_ops(), [
        Suggestion("#/paths/~1a/get/operationId", "b", "named"),
        Suggestion("#/paths/~1b/get/operationId", "a", "named"),
    ], rename_tools=True)
    assert out["x-s2o"]["minify"]["folded"] == {
        "b": ["errors"], "a": ["errors", "examples"]}


@pytest.mark.parametrize("wrapper", ["allOf", "oneOf", "anyOf"])
def test_example_is_not_written_on_a_composed_untyped_field(wrapper):
    """변환기는 형제 키가 있는 $ref 를 allOf 로 감싼다 - type 이 없는 합성
    필드도 type 을 확인할 수 없다."""
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["owner"] = {
        wrapper: [{"$ref": "#/components/schemas/Pet"}]}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/owner/description",
        "소유자", "named", example="바둑이 주인")])
    node = out["components"]["schemas"]["Pet"]["properties"]["owner"]
    assert node["description"] == "소유자"
    assert "example" not in node
    assert any("example" in r for r in rep.rejected)


@pytest.mark.parametrize("via", ["field", "pointer"])
def test_json_escaped_control_characters_are_cleaned_after_parsing(via):
    """정리는 파싱 전 원문에 적용된다. JSON 문자열 안의 유니코드 이스케이프는
    파싱 뒤에야 실제 ESC 가 된다 - 파싱한 값(키 포함)도 같은 정리를 받는다."""
    import json

    esc, bell = chr(27), chr(7)
    raw_list = json.dumps([esc + "[2Jevil", "a" + bell + "b"])
    raw_obj = json.dumps({esc + "[31mkey": esc + "[0mvalue"})
    assert esc not in raw_list and esc not in raw_obj  # 이스케이프된 원문
    spec = base_spec()
    props = spec["components"]["schemas"]["Pet"]["properties"]
    props["tags"] = {"type": "array", "items": {"type": "string"}}
    props["meta"] = {"type": "object"}
    base = "#/components/schemas/Pet/properties"
    if via == "field":
        sugs = [Suggestion(f"{base}/tags/description", "태그 목록", "named",
                           example=raw_list),
                Suggestion(f"{base}/meta/description", "메타", "named",
                           example=raw_obj)]
    else:
        sugs = [Suggestion(f"{base}/tags/example", raw_list, "named"),
                Suggestion(f"{base}/meta/example", raw_obj, "named")]
    out, _ = apply_suggestions(spec, sugs)
    got = out["components"]["schemas"]["Pet"]["properties"]
    assert got["tags"]["example"] == ["evil", "ab"]
    assert got["meta"]["example"] == {"key": "value"}


def test_example_is_not_written_next_to_a_ref():
    """$ref 필드는 type 을 확인할 수 없다 - example 을 붙이지 않는다."""
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["owner"] = {
        "$ref": "#/components/schemas/Pet"}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/owner/description",
        "소유자", "named", example="바둑이 주인")])
    node = out["components"]["schemas"]["Pet"]["properties"]["owner"]
    assert "example" not in node
    assert any("example" in r for r in rep.rejected)


def test_example_terminal_pointer_also_rejects_a_type_mismatch():
    """side channel(example=) 뿐 아니라 /example 로 끝나는 포인터
    경로도 같은 검사를 받아야 한다 (Ruling 53)."""
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["active"] = {
        "type": "boolean"}
    out, rep = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/active/example",
        "yes please", "named")])
    assert "example" not in out["components"]["schemas"]["Pet"][
        "properties"]["active"]
    assert any("example" in r for r in rep.rejected)


def test_example_terminal_pointer_parses_a_matching_numeric_string():
    spec = base_spec()
    spec["components"]["schemas"]["Pet"]["properties"]["qty"] = {
        "type": "integer"}
    out, _ = apply_suggestions(spec, [Suggestion(
        "#/components/schemas/Pet/properties/qty/example", "12", "named")])
    assert out["components"]["schemas"]["Pet"]["properties"]["qty"][
        "example"] == 12


def test_a_mismatched_example_is_rejected_on_a_31_nullable_type():
    """3.1 의 nullable 스칼라는 ["integer", "null"] 로 온다.

    리스트라는 이유로 통과시키면 이 함수가 막으라고 있는 바로 그 모순을
    3.1 스펙에서만 놓친다.
    """
    from spec2openapi.agentize.apply import _typed_example

    assert _typed_example("twelve", "integer") == (False, None)
    assert _typed_example("twelve", ["integer", "null"]) == (False, None)
    assert _typed_example("yes", ["boolean", "null"]) == (False, None)
    # 정상값은 그대로 통과하고 파싱까지 된다.
    assert _typed_example("12", ["integer", "null"]) == (True, 12)
    assert _typed_example("true", ["boolean", "null"]) == (True, True)


@pytest.mark.parametrize("raw", ["NaN", "nan", "Infinity", "-inf"])
def test_a_non_finite_example_is_rejected(raw):
    """float() 는 이 값들을 받아주지만 JSON 은 받아주지 않는다.

    통과시키면 dump_spec(fmt="json") 이 어떤 RFC 8259 파서도 읽지 못하는
    문서를 쓴다.
    """
    import json

    from spec2openapi.agentize.apply import _typed_example

    assert _typed_example(raw, "number") == (False, None)
    # 값이 통과했다면 json 이 깨졌을 것이라는 근거.
    with pytest.raises(ValueError):
        json.dumps(float(raw), allow_nan=False)


def test_a_trace_operation_pointer_is_allowed():
    """_METHOD 는 openapi.py 의 정본에서 파생한다.

    손으로 베낀 목록에 trace 가 빠져 있어, trace operation 은 LLM 호출
    비용만 치르고 제안이 전부 거부됐다.
    """
    from spec2openapi.agentize.apply import _pointer_allowed
    from spec2openapi.openapi import _HTTP_METHODS

    for method in _HTTP_METHODS:
        ptr = f"#/paths/~1x/{method}/description"
        assert _pointer_allowed(ptr, rename_tools=False), method
