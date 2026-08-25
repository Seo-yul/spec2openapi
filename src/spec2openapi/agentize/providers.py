"""provider 추상화와 테스트용 가짜 구현.

실제 SDK 어댑터는 anthropic_provider.py / openai_provider.py에 있고
사용 시점에 지연 import된다. `import spec2openapi`가 어떤 LLM SDK도
끌어오지 않아야 한다는 제약 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

#: 근거 등급. 앞의 셋만 기본 적용된다.
GROUNDING: tuple[str, ...] = ("named", "documented", "inferred", "speculative")
TRUSTED: frozenset[str] = frozenset({"named", "documented", "inferred"})


class ProviderError(RuntimeError):
    """provider 호출이 복구 불가능하게 실패했다."""


@dataclass(frozen=True)
class Suggestion:
    """모델이 제안한 한 곳의 문자열. 구조는 절대 담지 않는다."""

    pointer: str
    description: str | None
    grounding: str
    example: Any = None


@runtime_checkable
class EnrichProvider(Protocol):
    """구조화 출력을 돌려주는 최소 인터페이스.

    토큰 추정 메서드는 의도적으로 없다: --dry-run 은 provider 를 만들기
    전에 반환하는 무비용 경로이므로 API 호출이 필요한 추정이 들어갈
    자리가 없다. 비용 추정이 필요해지면 실제 호출 지점에 맞춰 새로
    설계한다.
    """

    name: str
    model: str

    def complete(self, system: str, user: str, schema: dict) -> dict:
        """schema에 맞는 dict를 돌려준다. 실패 시 ProviderError."""
        ...


class FakeProvider:
    """테스트용 결정론적 provider.

    responses: user 문자열 -> 응답 dict 매핑, 또는 user를 받는 콜러블.
    """

    name = "fake"
    model = "fake-1"

    def __init__(self, responses: dict[str, dict] | Callable[[str], dict]):
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append((system, user))
        if callable(self._responses):
            return self._responses(user)
        if user not in self._responses:
            raise ProviderError(f"FakeProvider: 매핑되지 않은 요청 {user!r}")
        return self._responses[user]


def resolve_provider_name(name: str | None = None) -> str:
    """어느 provider 를 쓸지 결정한다.

    우선순위: 인자 > SPEC2OPENAPI_LLM_PROVIDER > 존재하는 키 >
    anthropic. anthropic 이 기본인 이유는 프로필(`ant auth login`)
    인증이 가능해 환경변수 키가 없어도 성립하기 때문이다.
    """
    import os

    name = (name or os.environ.get("SPEC2OPENAPI_LLM_PROVIDER") or "").strip()
    if name:
        return name
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "anthropic"


def resolve_provider(name: str | None = None, model: str | None = None):
    """이름/환경변수로 provider 를 만든다.

    CLI 와 평가 하네스가 함께 쓴다. 선택 규칙을 양쪽에 따로 두면
    한쪽만 고쳐지고 갈라진다 - 실제로 하네스가 anthropic 으로
    고정돼 있어 OpenAI 키만 있는 환경에서는 돌지 않았다.
    """
    import os

    name = resolve_provider_name(name)
    model = model or os.environ.get("SPEC2OPENAPI_LLM_MODEL") or None

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model)
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(model=model)
    raise ValueError(f"알 수 없는 provider: {name!r}")
