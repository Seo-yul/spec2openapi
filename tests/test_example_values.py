"""Deterministic, source-derived example values on WSDL schemas (#117)."""
from __future__ import annotations

import pytest

from spec2openapi import convert_wsdl

validate = pytest.importorskip("openapi_spec_validator").validate


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.fixture(scope="module")
def orders():
    return convert_wsdl("tests/fixtures/orders.wsdl")


def test_enum_example_is_first_enumeration_value(orders):
    hits = [n for n in _walk(orders) if n.get("enum") and "example" in n]
    assert hits and all(n["example"] == n["enum"][0] for n in hits)


def test_datetime_format_gets_canonical_example(orders):
    hits = [n for n in _walk(orders)
            if n.get("format") == "date-time" and n.get("type") == "string"]
    assert hits and all(
        n.get("example") == "2024-01-15T10:30:00Z" for n in hits)


@pytest.mark.parametrize("fixture", ["orders", "advanced", "stress"])
def test_no_example_next_to_default(fixture):
    spec = convert_wsdl(f"tests/fixtures/{fixture}.wsdl")
    assert not [n for n in _walk(spec) if "default" in n and "example" in n]


def test_plain_strings_get_no_invented_example(orders):
    # no synthesis: a bare string without enum/format stays example-free
    plain = [n for n in _walk(orders)
             if n.get("type") == "string" and "enum" not in n
             and n.get("format") not in ("date-time", "date", "time",
                                         "duration")]
    assert plain and all("example" not in n for n in plain)
