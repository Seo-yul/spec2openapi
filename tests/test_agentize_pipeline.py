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
