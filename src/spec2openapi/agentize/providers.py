"""provider 추상화와 테스트용 가짜 구현.

실제 SDK 어댑터는 anthropic_provider.py / openai_provider.py에 있고
사용 시점에 지연 import된다. `import spec2openapi`가 어떤 LLM SDK도
끌어오지 않아야 한다는 제약 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

#: 근거 등급. 앞의 셋만 기본 적용된다.
GROUNDING = ("named", "documented", "inferred", "speculative")
TRUSTED = frozenset({"named", "documented", "inferred"})


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
    """구조화 출력을 돌려주는 최소 인터페이스."""

    name: str
    model: str

    def complete(self, system: str, user: str, schema: dict) -> dict:
        """schema에 맞는 dict를 돌려준다. 실패 시 ProviderError."""
        ...

    def count_tokens(self, system: str, user: str) -> int:
        """요청의 입력 토큰 수를 돌려준다 (--dry-run 추정용)."""
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

    def count_tokens(self, system: str, user: str) -> int:
        # 실제 토크나이저가 아니라 결정론적 대용치. --dry-run 테스트용.
        return (len(system) + len(user)) // 4
