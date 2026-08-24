"""Anthropic SDK 어댑터.

구조화 출력은 tool use + strict 로 강제한다. 프리텍스트를 파싱하지
않으므로 파싱 실패 재시도 로직이 필요 없다.

시스템 프롬프트에는 우리 지시만 들어가고 외부 스펙 데이터는 반드시
user 메시지로 간다. 캐시 prefix 안정성과 신뢰 경계 둘 다를 위한 것이다.
"""
from __future__ import annotations

from typing import Any

from .providers import ProviderError

DEFAULT_MODEL = "claude-opus-5"
_TOOL_NAME = "emit_descriptions"
_MAX_TOKENS = 16000


class AnthropicProvider:
    """EnrichProvider 구현."""

    name = "anthropic"

    def __init__(self, model: str | None = None, client: Any = None):
        self.model = model or DEFAULT_MODEL
        if client is not None:
            self._client = client
            return
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "agentize --provider anthropic requires the SDK; install it "
                "with: pip install 'spec2openapi[llm-anthropic]'") from exc
        # 키를 미리 검사하지 않는다: `ant auth login` 프로필로도 동작한다.
        self._client = anthropic.Anthropic()

    def _tools(self, schema: dict) -> list[dict]:
        return [{
            "name": _TOOL_NAME,
            "description": "생성한 설명 문자열을 구조화해서 돌려준다.",
            "input_schema": schema or {"type": "object"},
            "strict": True,
        }]

    def complete(self, system: str, user: str, schema: dict) -> dict:
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                tools=self._tools(schema),
                tool_choice={"type": "tool", "name": _TOOL_NAME},
            )
        except (TypeError, AttributeError, KeyError):
            # 우리 코드의 버그다. provider 장애로 둔갑시키면 진단이
            # 불가능해진다 - SDK 장애와 구분되어야 한다.
            raise
        except Exception as exc:
            raise ProviderError(f"anthropic 호출 실패: {exc}") from exc

        for block in getattr(msg, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        raise ProviderError(
            f"anthropic 응답에 tool_use 블록이 없다 "
            f"(stop_reason={getattr(msg, 'stop_reason', None)!r})")
