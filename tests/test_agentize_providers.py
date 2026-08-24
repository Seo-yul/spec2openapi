"""provider 어댑터: 요청 구조와 캐시 경계를 검증한다 (실제 호출 없음)."""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


class _StubMessages:
    def __init__(self, payload):
        self.payload, self.seen = payload, []

    def create(self, **kw):
        self.seen.append(kw)

        class _Block:
            type = "tool_use"
            input = self.payload

        class _Msg:
            content = [_Block()]
            stop_reason = "tool_use"

        return _Msg()


class _StubClient:
    def __init__(self, payload):
        self.messages = _StubMessages(payload)


def test_anthropic_returns_tool_input_as_dict():
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    client = _StubClient({"fields": {"n": {"description": "d",
                                           "grounding": "named"}}})
    p = AnthropicProvider(client=client)
    got = p.complete("SYS", "USER", {"type": "object"})
    assert got["fields"]["n"]["grounding"] == "named"
    assert p.name == "anthropic"
    assert p.model == "claude-opus-5"


def test_anthropic_puts_external_data_in_user_not_system():
    """신뢰 경계: 외부 스펙 데이터는 system 프롬프트에 들어가면 안 된다."""
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    client = _StubClient({"fields": {}})
    AnthropicProvider(client=client).complete("SYS-ONLY", "EXTERNAL", {})
    sent = client.messages.seen[0]
    assert "EXTERNAL" not in str(sent["system"])
    assert "EXTERNAL" in str(sent["messages"])


def test_anthropic_marks_system_for_caching():
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    client = _StubClient({"fields": {}})
    AnthropicProvider(client=client).complete("SYS", "USER", {})
    system = client.messages.seen[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_forces_the_structured_output_tool():
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    client = _StubClient({"fields": {}})
    AnthropicProvider(client=client).complete("SYS", "USER",
                                              {"type": "object"})
    sent = client.messages.seen[0]
    assert sent["tools"][0]["strict"] is True
    assert sent["tool_choice"]["type"] == "tool"


def test_anthropic_raises_provider_error_when_no_tool_use():
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider
    from spec2openapi.agentize.providers import ProviderError

    class _Empty:
        class messages:
            @staticmethod
            def create(**kw):
                class _M:
                    content = []
                    stop_reason = "end_turn"
                return _M()

    with pytest.raises(ProviderError):
        AnthropicProvider(client=_Empty()).complete("S", "U", {})


def test_importing_spec2openapi_does_not_pull_llm_sdks():
    """Global Constraint: 기본 import가 LLM SDK를 끌어오면 안 된다."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import sys
            import spec2openapi
            from spec2openapi.agentize import agentize_spec  # noqa: F401
            print([m for m in ("anthropic", "openai") if m in sys.modules])
        """)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_missing_sdk_raises_import_error_naming_the_extra(monkeypatch):
    """SDK 가 없으면 설치 방법을 담은 ImportError 를 낸다 - 이 메시지가
    그대로 CLI 의 stderr 로 나가 사용자에게 보인다."""
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.delitem(sys.modules,
                        "spec2openapi.agentize.anthropic_provider",
                        raising=False)
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    with pytest.raises(ImportError, match="llm-anthropic"):
        AnthropicProvider()


def test_local_programming_errors_are_not_disguised_as_provider_failures():
    """우리 코드의 버그를 ProviderError 로 감싸면 SDK 장애와 구분되지
    않아 진단이 불가능해진다."""
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    class _Broken:
        class messages:
            @staticmethod
            def create(**kw):
                raise TypeError("create() got an unexpected keyword argument")

    with pytest.raises(TypeError):
        AnthropicProvider(client=_Broken()).complete("S", "U", {})


def test_the_schema_argument_reaches_the_tool_definition():
    """provider 가 schema 를 조용히 무시하면 구조화 출력이 강제되지
    않는다 - 그리고 그 결함은 다른 테스트로는 드러나지 않는다."""
    from spec2openapi.agentize.anthropic_provider import AnthropicProvider

    client = _StubClient({"fields": {}})
    schema = {"type": "object", "properties": {"x": {"type": "string"}},
              "required": ["x"], "additionalProperties": False}
    AnthropicProvider(client=client).complete("SYS", "USER", schema)
    assert client.messages.seen[0]["tools"][0]["input_schema"] == schema


# --- OpenAI -----------------------------------------------------------------

class _OAIStub:
    def __init__(self, payload):
        self.payload, self.seen = payload, []
        outer = self

        class _Completions:
            @staticmethod
            def create(**kw):
                outer.seen.append(kw)
                import json as _json

                class _Msg:
                    content = _json.dumps(outer.payload)

                class _Choice:
                    message = _Msg()

                class _R:
                    choices = [_Choice()]

                return _R()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_openai_parses_structured_output():
    from spec2openapi.agentize.openai_provider import OpenAIProvider

    client = _OAIStub({"fields": {"n": {"description": "d",
                                        "grounding": "inferred"}}})
    p = OpenAIProvider(model="gpt-5", client=client)
    got = p.complete("SYS", "USER", {"type": "object"})
    assert got["fields"]["n"]["grounding"] == "inferred"
    assert p.name == "openai"


def test_openai_keeps_external_data_out_of_system_role():
    from spec2openapi.agentize.openai_provider import OpenAIProvider

    client = _OAIStub({"fields": {}})
    OpenAIProvider(model="gpt-5", client=client).complete(
        "SYS-ONLY", "EXTERNAL", {})
    msgs = client.seen[0]["messages"]
    system = [m for m in msgs if m["role"] == "system"]
    user = [m for m in msgs if m["role"] == "user"]
    assert "EXTERNAL" not in str(system)
    assert "EXTERNAL" in str(user)


def test_openai_requests_strict_json_schema():
    from spec2openapi.agentize.openai_provider import OpenAIProvider

    client = _OAIStub({"fields": {}})
    OpenAIProvider(model="gpt-5", client=client).complete(
        "S", "U", {"type": "object"})
    fmt = client.seen[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True


def test_openai_raises_provider_error_on_bad_json():
    from spec2openapi.agentize.openai_provider import OpenAIProvider
    from spec2openapi.agentize.providers import ProviderError

    class _Bad(_OAIStub):
        pass

    client = _Bad({})
    client.chat.completions.create = lambda **kw: type(
        "R", (), {"choices": [type("C", (), {"message": type(
            "M", (), {"content": "not json"})()})()]})()
    with pytest.raises(ProviderError):
        OpenAIProvider(model="gpt-5", client=client).complete("S", "U", {})


def test_openai_passes_the_schema_through_to_json_schema():
    """provider 가 schema 를 조용히 무시하면 구조화 출력이 강제되지
    않는다 - 그리고 그 결함은 다른 테스트로는 드러나지 않는다."""
    from spec2openapi.agentize.openai_provider import OpenAIProvider

    client = _OAIStub({"fields": {}})
    schema = {"type": "object", "properties": {"x": {"type": "string"}},
              "required": ["x"], "additionalProperties": False}
    OpenAIProvider(model="gpt-5", client=client).complete(
        "SYS", "USER", schema)
    assert client.seen[0]["response_format"]["json_schema"]["schema"] == schema


def test_openai_local_programming_errors_propagate():
    """우리 코드의 버그를 ProviderError 로 감싸면 SDK 장애와 구분되지
    않아 진단이 불가능해진다."""
    from spec2openapi.agentize.openai_provider import OpenAIProvider

    class _Broken:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise TypeError("create() got an unexpected keyword")

    with pytest.raises(TypeError):
        OpenAIProvider(model="gpt-5", client=_Broken()).complete("S", "U", {})


def test_openai_requires_an_explicit_model():
    """확인되지 않은 기본 모델을 추정하지 않는다 (Ruling 62) - 잘못된
    기본값은 --model 을 넘기지 않은 모든 사용자에게 첫 호출부터 404 를
    내는 provider 를 만든다."""
    from spec2openapi.agentize.openai_provider import OpenAIProvider

    with pytest.raises(ValueError, match="--model"):
        OpenAIProvider(client=_OAIStub({}))


def test_openai_module_does_not_define_a_default_model():
    """DEFAULT_MODEL 자체를 모듈에서 없앤다 - 남아 있으면 나중에 누군가
    다시 기본값으로 쓰게 된다."""
    from spec2openapi.agentize import openai_provider

    assert not hasattr(openai_provider, "DEFAULT_MODEL")
