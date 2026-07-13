"""Regression tests for core-converter correctness bugs (#8)."""
from __future__ import annotations

from pathlib import Path

from spec2openapi import convert_swagger, convert_wsdl
from spec2openapi.openapi import to_openapi_31

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EDGE = str(FIXTURES / "edgecases.wsdl")


# -- WSDL / schema -----------------------------------------------------------

def test_operationid_collision_keeps_both_operations():
    """Get.Data and Get-Data both sanitize to Get_Data — neither may be lost."""
    spec = convert_wsdl(EDGE)
    op_ids = {
        p["post"]["operationId"] for p in spec["paths"].values() if "post" in p
    }
    # three operations in, three out (none silently overwritten)
    assert len(spec["paths"]) == 3
    assert len(op_ids) == 3
    assert "Get_Data" in op_ids and "Get_Data_2" in op_ids
    # the two soapActions prove both distinct operations survived
    actions = {p["post"]["x-soap"]["soapAction"] for p in spec["paths"].values()}
    assert {"urn:GetDotData", "urn:GetDashData"} <= actions


def test_top_level_choice_detected():
    spec = convert_wsdl(EDGE)
    schema = _request_schema(spec, "Get_Data")  # TopChoice input
    assert "x-soap-choice" in schema
    members = {m for g in schema["x-soap-choice"] for m in g["members"]}
    assert members == {"cash", "card"}
    # choice members are not required
    assert not set(schema.get("required", [])) & members


def test_sequence_branch_choice_not_all_required():
    spec = convert_wsdl(EDGE)
    schema = _request_schema(spec, "Get_Data_2")  # SeqChoice input
    # the impossible "all three required" must not happen
    assert "cardNumber" not in schema.get("required", [])
    assert "iban" not in schema.get("required", [])
    members = {m for g in schema["x-soap-choice"] for m in g["members"]}
    assert {"cardNumber", "cardExpiry", "iban"} <= members


def test_wsdl_type_named_soapfault_not_clobbered():
    spec = convert_wsdl(EDGE)
    schemas = spec["components"]["schemas"]
    # the user's SoapFault (errorCode/errorDetail) must survive under some name
    user_faults = [
        n for n, s in schemas.items()
        if "errorCode" in (s.get("properties") or {})
    ]
    assert user_faults, "user-defined SoapFault type was clobbered"
    # and the built-in fault schema (faultcode/faultstring) still exists too
    builtin = [
        n for n, s in schemas.items()
        if "faultcode" in (s.get("properties") or {})
    ]
    assert builtin


def test_attribute_name_clash_uses_real_name():
    spec = convert_wsdl(EDGE)
    schema = _request_schema(spec, "Clash")
    # find the attribute property (xml.attribute == True)
    attrs = {
        name: p for name, p in schema["properties"].items()
        if p.get("xml", {}).get("attribute")
    }
    assert attrs
    for p in attrs.values():
        assert p["xml"]["name"] == "id"  # not zeep's mangled "attr__id"


# -- Swagger upgrader --------------------------------------------------------

def test_operationid_dedup_respects_64_chars():
    a = "a" * 70
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {"get": {"operationId": a, "responses": {"200": {"description": "ok"}}}},
            "/y": {"get": {"operationId": a + "b", "responses": {"200": {"description": "ok"}}}},
        },
    }
    out = convert_swagger(spec)
    ids = [op["get"]["operationId"] for op in out["paths"].values()]
    assert len(set(ids)) == 2
    assert all(len(i) <= 64 for i in ids)


def test_empty_operationid_falls_back():
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"get": {"operationId": "!!!", "responses": {"200": {"description": "ok"}}}}},
    }
    out = convert_swagger(spec)
    oid = out["paths"]["/x"]["get"]["operationId"]
    assert oid  # non-empty


def test_operation_param_overrides_path_param():
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "parameters": [{"name": "id", "in": "query", "type": "string"}],
                "get": {
                    "parameters": [{"name": "id", "in": "query", "type": "integer"}],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
    }
    out = convert_swagger(spec)
    params = out["paths"]["/x"]["get"]["parameters"]
    ids = [p for p in params if p.get("name") == "id"]
    assert len(ids) == 1  # no duplicate
    assert ids[0]["schema"]["type"] == "integer"  # op-level won


def test_basepath_without_leading_slash():
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "host": "api.example.com",
        "basePath": "v2",
        "paths": {},
    }
    out = convert_swagger(spec)
    assert out["servers"][0]["url"] == "https://api.example.com/v2"


def test_example_data_not_rewritten():
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "definitions": {
            "T": {
                "type": "object",
                "example": {"discriminator": "petType", "x-nullable": True},
            }
        },
    }
    out = convert_swagger(spec)
    ex = out["components"]["schemas"]["T"]["example"]
    assert ex == {"discriminator": "petType", "x-nullable": True}


def test_exclusive_minimum_boolean_stripped_in_31():
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "definitions": {
            "T": {"type": "number", "minimum": 0, "exclusiveMinimum": False}
        },
    }
    out = convert_swagger(spec, openapi_version="3.1")
    t = out["components"]["schemas"]["T"]
    # 2020-12: exclusiveMinimum must be a number or absent, never boolean
    assert not isinstance(t.get("exclusiveMinimum"), bool)
    assert t["minimum"] == 0


def test_paths_level_vendor_extension_preserved_not_pathified():
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "x-hidden": {"note": "meta"},
            "/a": {"get": {"operationId": "a", "responses": {"200": {"description": "ok"}}}},
        },
    }
    out = convert_swagger(spec)
    assert out["paths"]["x-hidden"] == {"note": "meta"}
    assert "operationId" not in out["paths"]["x-hidden"]


def test_path_param_forced_required():
    """OpenAPI 3 requires path params to be required:true (#15)."""
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/u/{id}": {
                "get": {
                    "operationId": "g",
                    "parameters": [
                        {"name": "id", "in": "path", "type": "string"},  # no required
                        {"name": "q", "in": "query", "type": "string"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    out = convert_swagger(spec)
    params = {p["name"]: p for p in out["paths"]["/u/{id}"]["get"]["parameters"]}
    assert params["id"]["required"] is True
    # a non-path param without `required` is left as-is (defaults false)
    assert params["q"].get("required") is not True
    assert any("path parameter 'id'" in a for a in out["x-s2o"]["assumptions"])


def test_two_body_params_recorded_lossy():
    spec = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a": {
                "post": {
                    "operationId": "a",
                    "parameters": [
                        {"name": "b1", "in": "body", "schema": {"type": "string"}},
                        {"name": "b2", "in": "body", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    out = convert_swagger(spec)
    assert any("body" in m.lower() for m in out["x-s2o"]["lossy"])


# -- helpers -----------------------------------------------------------------

def _request_schema(spec, op_id):
    for p in spec["paths"].values():
        post = p.get("post")
        if post and post["operationId"] == op_id:
            return post["requestBody"]["content"]["application/json"]["schema"]
    raise AssertionError(f"operation {op_id} not found")
