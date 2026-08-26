"""OpenAI SDK 어댑터.

네이티브 structured outputs(json_schema + strict)를 쓴다. OpenAI 호환
shim으로 다른 provider를 부르지 않는다.

시스템 프롬프트에는 우리 지시만 들어가고 외부 스펙 데이터는 반드시
user 메시지로 간다. 캐시 prefix 안정성과 신뢰 경계 둘 다를 위한 것이다.
"""
from __future__ import annotations

import json
from typing import Any

from .providers import ProviderError

_SCHEMA_NAME = "descriptions"


class OpenAIProvider:
    """EnrichProvider 구현.

    기본 모델을 추정하지 않는다 (Ruling 62) - 확인되지 않은 기본값은
    --model 을 넘기지 않은 모든 사용자에게 첫 호출부터 404 를 내는
    "죽어서 도착하는" provider 를 만든다. anthropic 은 문서로 검증된
    기본값이 있어 예외지만, openai 는 그런 검증이 없었으므로 짐작
    대신 명시적인 에러로 --model 을 요구한다.
    """

    name = "openai"

    def __init__(self, model: str | None = None, client: Any = None):
        if not model:
            raise ValueError(
                "openai provider 는 기본 모델을 추정하지 않는다; "
                "--model 로 사용할 모델 ID를 직접 지정하라")
        self.model = model
        if client is not None:
            self._client = client
            return
        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "agentize --provider openai requires the SDK; install it "
                "with: pip install 'spec2openapi[llm-openai]'") from exc
        # 키를 미리 검사하지 않는다: 실패는 실제 호출 시점에 판단한다.
        self._client = openai.OpenAI()

    def complete(self, system: str, user: str, schema: dict) -> dict:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": _SCHEMA_NAME, "strict": True,
                                    "schema": schema or {"type": "object"}},
                },
            )
        except (TypeError, AttributeError, KeyError):
            # 우리 코드의 버그다. provider 장애로 둔갑시키면 진단이
            # 불가능해진다 - SDK 장애와 구분되어야 한다.
            raise
        except Exception as exc:
            raise ProviderError(f"openai 호출 실패: {exc}") from exc
        try:
            return json.loads(resp.choices[0].message.content)
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"openai 응답을 해석할 수 없다: {exc}") from exc
