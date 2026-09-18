"""digest(): LLM에게 보낼 입력을 화이트리스트로 압축한다."""
from __future__ import annotations

import json

from spec2openapi.agentize.prompts import (
    digest_operation,
    digest_schema,
    spec_outline,
)


def test_extensions_and_xml_are_excluded():
    node = {"type": "string", "description": "이름",
            "xml": {"name": "n", "namespace": "http://x"},
            "x-soap-substitution": {"a": 1}, "x-anything": 2}
    assert digest_schema(node, {}) == {"type": "string", "description": "이름"}


def test_unknown_keys_are_excluded_by_whitelist():
    node = {"type": "string", "brandNewKeyword2030": {"deep": "junk"}}
    assert digest_schema(node, {}) == {"type": "string"}


def test_constraints_and_enum_survive():
    node = {"type": "integer", "enum": [1, 2], "minimum": 0, "maximum": 9,
            "format": "int32", "default": 1, "pattern": "^x", "title": "T",
            "minLength": 1, "maxLength": 4, "nullable": True}
    assert digest_schema(node, {}) == node


def test_ref_is_inlined():
    spec = {"components": {"schemas": {
        "Pet": {"type": "object",
                "properties": {"name": {"type": "string"}}}}}}
    got = digest_schema({"$ref": "#/components/schemas/Pet"}, spec)
    assert got == {"type": "object", "properties": {"name": {"type": "string"}}}


def test_recursive_ref_is_cut_not_infinite():
    spec = {"components": {"schemas": {"Node": {
        "type": "object",
        "properties": {"child": {"$ref": "#/components/schemas/Node"}}}}}}
    got = digest_schema({"$ref": "#/components/schemas/Node"}, spec)
    assert got["properties"]["child"] == {"$ref": "Node"}


def test_depth_is_bounded():
    node = cur = {"type": "object"}
    for _ in range(40):
        cur["properties"] = {"n": {"type": "object"}}
        cur = cur["properties"]["n"]
    got = digest_schema(node, {})
    assert json.dumps(got)  # 재귀 폭주 없이 직렬화된다


def test_digest_operation_drops_x_soap():
    op = {"operationId": "CreateOrder", "summary": "주문 생성",
          "x-soap": {"soapAction": "http://x/Create", "endpoint": "http://y"},
          "requestBody": {"content": {"application/json": {
              "schema": {"type": "object",
                         "properties": {"note": {"type": "string"}}}}}},
          "parameters": [{"name": "trace", "in": "header",
                          "schema": {"type": "string"}}]}
    got = digest_operation("/operations/CreateOrder", "post", op, {})
    assert "x-soap" not in json.dumps(got)
    assert got["operationId"] == "CreateOrder"
    assert got["input"]["properties"]["note"] == {"type": "string"}
    assert got["parameters"][0]["name"] == "trace"


def test_spec_outline_carries_names_only():
    spec = {"info": {"title": "OrderService"},
            "paths": {"/o": {"post": {"operationId": "CreateOrder"}}},
            "components": {"schemas": {"Item": {"type": "object",
                "properties": {"sku": {"type": "string"},
                               "qty": {"type": "integer"}}}}}}
    got = spec_outline(spec)
    assert got["service"] == "OrderService"
    assert got["operations"] == ["CreateOrder"]
    assert got["types"] == {"Item": ["sku", "qty"]}
    assert "type" not in json.dumps(got["types"])


def test_digest_actually_shrinks_a_real_wsdl_conversion(orders_wsdl):
    from spec2openapi import convert_wsdl
    from spec2openapi.openapi import _operations

    spec = convert_wsdl(orders_wsdl)
    raw = dig = 0
    for path, method, op in _operations(spec):
        raw += len(json.dumps({"path": path, **op}, ensure_ascii=False))
        dig += len(json.dumps(digest_operation(path, method, op, spec),
                              ensure_ascii=False))
    assert dig < raw * 0.4, f"절감 부족: raw={raw} digest={dig}"


def test_digest_operation_keeps_refs_short_instead_of_inlining():
    """pass 2b는 공유 스키마 본문을 인라인하지 않는다 - pass 2a가 스키마당
    1회 처리하므로 operation마다 펼치면 입력이 재사용 배수만큼 중복된다."""
    spec = {"components": {"schemas": {"Pet": {
        "type": "object",
        "properties": {"name": {"type": "string"},
                       "tag": {"type": "string"}}}}}}
    op = {"operationId": "createPet",
          "requestBody": {"content": {"application/json": {
              "schema": {"$ref": "#/components/schemas/Pet"}}}}}
    got = digest_operation("/pets", "post", op, spec)
    assert got["input"] == {"$ref": "Pet"}


def test_non_dict_additional_properties_survives():
    """additionalProperties: false는 '추가 필드 금지'라는 의미 정보다 -
    bool이라는 이유로 떨어뜨리면 모델이 그 제약을 알 수 없다."""
    node = {"type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False}
    assert digest_schema(node, {})["additionalProperties"] is False


def test_dict_additional_properties_is_still_digested():
    node = {"type": "object",
            "additionalProperties": {"type": "string", "xml": {"name": "x"}}}
    assert digest_schema(node, {})["additionalProperties"] == {"type": "string"}


def test_pattern_properties_recurses_like_properties():
    node = {"type": "object",
            "patternProperties": {"^x-": {"type": "string",
                                          "xml": {"name": "n"}}}}
    assert digest_schema(node, {}) == {
        "type": "object", "patternProperties": {"^x-": {"type": "string"}}}
