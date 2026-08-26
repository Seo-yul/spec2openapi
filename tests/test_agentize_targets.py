"""targets.py: 무엇을 보강 대상으로 볼 것인가 (LLM 관여 없음)."""
from __future__ import annotations

import pytest

from spec2openapi.agentize.targets import (
    KINDS,
    Target,
    detect_language,
    find_targets,
    low_value_reason,
    normalize,
)


def _spec(**kw):
    base = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {}, "components": {"schemas": {}}}
    base.update(kw)
    return base


def test_normalize_strips_case_and_punctuation():
    assert normalize("Create Order!") == "createorder"
    assert normalize(None) == ""


@pytest.mark.parametrize("desc,name,sibling,expected", [
    (None, "email", None, "empty"),
    ("", "email", None, "empty"),
    ("   ", "email", None, "empty"),
    ("Create order", "createOrder", None, "restatement"),
    ("Lists pets", "listPets", "Lists pets", "duplicate"),
    ("short", "email", None, "too-short"),
    ("주문 확인을 보낼 연락처", "email", None, None),
])
def test_low_value_reason(desc, name, sibling, expected):
    assert low_value_reason(desc, name=name, sibling=sibling) == expected


def test_empty_property_description_is_a_target():
    spec = _spec(components={"schemas": {
        "Pet": {"type": "object", "properties": {
            "name": {"type": "string"},
            "tag": {"type": "string", "description": "분류용 자유 태그 문자열"},
        }}}})
    got = find_targets(spec, kinds=("properties",))
    assert [(t.pointer, t.reason) for t in got] == [
        ("#/components/schemas/Pet/properties/name/description", "empty")]


def test_operation_with_good_summary_and_no_description_is_not_a_target():
    """FastMCP는 description이 없으면 summary를 서빙한다 (minify.py 참조).
    그런 operation은 agent에게 이미 충분하므로 대상이 아니다."""
    spec = _spec(paths={"/pets": {"get": {
        "operationId": "listPets",
        "summary": "이름과 태그로 반려동물 목록을 조회한다",
        "responses": {"200": {"description": "ok"}},
    }}})
    assert find_targets(spec, kinds=("desc",)) == []


def test_operation_with_duplicated_summary_and_description_is_a_target():
    spec = _spec(paths={"/pets": {"get": {
        "operationId": "listPets",
        "summary": "이름과 태그로 반려동물 목록을 조회한다",
        "description": "이름과 태그로 반려동물 목록을 조회한다",
        "responses": {"200": {"description": "ok"}},
    }}})
    got = find_targets(spec, kinds=("desc",))
    assert [(t.pointer, t.reason) for t in got] == [
        ("#/paths/~1pets/get/description", "duplicate")]


def test_parameter_without_description_is_a_target():
    spec = _spec(paths={"/pets": {"get": {
        "operationId": "listPets",
        "summary": "반려동물 목록을 조회한다",
        "parameters": [{"name": "limit", "in": "query",
                        "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "ok"}},
    }}})
    got = find_targets(spec, kinds=("params",))
    assert [(t.pointer, t.name) for t in got] == [
        ("#/paths/~1pets/get/parameters/0/description", "limit")]


def test_pointer_tokens_are_rfc6901_escaped():
    spec = _spec(paths={"/pets/{id}": {"get": {
        "operationId": "getPet", "summary": "",
        "responses": {"200": {"description": "ok"}}}}})
    got = find_targets(spec, kinds=("desc",))
    assert got[0].pointer == "#/paths/~1pets~1{id}/get/description"


def test_policy_empty_only_skips_low_value_but_present():
    spec = _spec(components={"schemas": {
        "Pet": {"type": "object", "properties": {"name": {
            "type": "string", "description": "name"}}}}})
    assert find_targets(spec, kinds=("properties",), policy="default")
    assert find_targets(spec, kinds=("properties",), policy="empty-only") == []


def test_policy_overwrite_targets_everything():
    spec = _spec(components={"schemas": {
        "Pet": {"type": "object", "properties": {"name": {
            "type": "string", "description": "반려동물의 표시용 이름"}}}}})
    assert find_targets(spec, kinds=("properties",), policy="default") == []
    got = find_targets(spec, kinds=("properties",), policy="overwrite")
    assert [t.reason for t in got] == ["overwrite"]


def test_enums_has_no_producer_and_is_not_a_kind():
    """"enums"는 KINDS에서 빠져 있어야 한다 - find_targets()가 만들어낸
    적이 없는데도 CLI 기본값에 실려 있던 죽은 옵션이었다 (Ruling 54).
    enum 의미는 property description(=properties kind)에 문장으로
    들어간다."""
    assert "enums" not in KINDS
    assert KINDS == ("properties", "desc", "params", "examples")


def test_examples_kind_produces_no_targets():
    """"examples"는 find_targets()가 만들어내는 대상 종류가 아니라
    permission이다 (Ruling 54) - properties의 부산물인 example 적용
    여부를 결정할 뿐, 그 자체로는 아무 대상도 만들지 않는다."""
    spec = _spec(components={"schemas": {
        "Pet": {"type": "object", "properties": {
            "name": {"type": "string"}}}}})
    assert find_targets(spec, kinds=("examples",)) == []


def test_kind_filter_excludes_unrequested_kinds():
    spec = _spec(
        paths={"/pets": {"get": {"operationId": "listPets",
                                 "responses": {"200": {"description": "ok"}}}}},
        components={"schemas": {"Pet": {"type": "object",
                                        "properties": {"name": {"type": "string"}}}}})
    got = find_targets(spec, kinds=("properties",))
    assert all(t.kind == "properties" for t in got)
    assert got and isinstance(got[0], Target)


def test_minify_folds_do_not_make_an_operation_look_documented():
    """minify_for_mcp 가 접어 넣은 "Errors: ..." 줄은 "이 operation 이
    무엇을 하는가"에 대한 답이 아니다. 그것까지 설명으로 세면 설명이
    전혀 없는 operation 이 이미 문서화된 것처럼 보여 대상에서 빠진다."""
    spec = _spec(paths={"/pets/{id}": {"get": {
        "operationId": "getPet",
        "description": "Errors: 404 (Resource not found).",
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}],
        "responses": {"200": {"description": "ok"}}}}})
    spec["x-s2o"] = {"minify": {"folded": {"getPet": ["errors"]}}}
    got = find_targets(spec, kinds=("desc",))
    assert [(t.pointer, t.reason) for t in got] == [
        ("#/paths/~1pets~1{id}/get/description", "empty")]


def test_a_user_written_marker_line_is_not_stripped():
    """folded 기록이 없으면 marker 모양 줄도 사용자 문서다 - 벗기지 않는다."""
    spec = _spec(paths={"/pets/{id}": {"get": {
        "operationId": "getPet",
        "description": "Errors: 우리 팀이 직접 쓴 에러 안내 문서입니다",
        "responses": {"200": {"description": "ok"}}}}})
    assert find_targets(spec, kinds=("desc",)) == []


class TestDetectLanguage:
    """출력 언어는 우리가 정한다.

    모델에게 "문서에 이미 쓰인 언어로 써라"라고 맡겼더니 같은 스펙,
    같은 모델로 두 번 돌린 결과가 영어와 한국어로 갈렸다. 문장 자체는
    멀쩡해서 verify() 도 사람 눈도 그냥 지나쳤다.
    """

    def test_an_english_spec_is_english(self):
        assert detect_language({"info": {
            "title": "Pet Store",
            "description": "The service creates and retrieves pet records "
                           "for a shop, with support for tags."}}) == "English"

    def test_a_korean_spec_is_korean(self):
        assert detect_language({"info": {
            "title": "주문 서비스",
            "description": "주문을 생성하고 조회하는 서비스이다."}}) == "Korean"

    def test_a_french_spec_is_french(self):
        assert detect_language({"info": {
            "title": "Service de commande",
            "description": "Le service permet de créer et de consulter les "
                           "commandes pour une boutique."}}) == "French"

    def test_identifiers_are_not_evidence_of_language(self):
        """한국어 스펙도 필드명은 영어로 짓는다.

        필드명을 근거로 삼으면 거의 모든 스펙이 영어로 판정된다.
        """
        spec = {"info": {"title": "결제 서비스",
                         "description": "카드와 계좌이체를 처리한다."},
                "paths": {"/payments": {"post": {
                    "operationId": "createPaymentTransaction",
                    "responses": {"200": {"description": "성공"}}}}},
                "components": {"schemas": {"PaymentRequest": {
                    "type": "object",
                    "properties": {"customerIdentifier": {"type": "string"},
                                   "transactionAmount": {"type": "integer"}}}}}}
        assert detect_language(spec) == "Korean"

    def test_a_spec_with_no_prose_falls_back_to_english(self):
        assert detect_language({"info": {}}) == "English"
        assert detect_language({}) == "English"

    def test_detection_survives_having_the_descriptions_stripped(self):
        """평가 하네스는 property 설명을 지운 뒤에 돌린다.

        그 상태에서 판정이 흔들리면 하네스가 매번 다른 언어를 요구한다.
        """
        spec = {"info": {"title": "Pet Store",
                         "description": "The service manages the pets of a "
                                        "shop and the tags for each of them."},
                "components": {"schemas": {"Pet": {"type": "object",
                    "properties": {"name": {"type": "string"}}}}}}
        assert detect_language(spec) == "English"
