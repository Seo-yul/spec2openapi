"""LLM 기반 agent-facing tool 표면 보강 (선택 기능).

이 패키지는 CLI의 `agentize` 명령이 소비한다. import 시점에 어떤 LLM
SDK도 끌어오지 않는다 - provider 어댑터는 실제 사용 시점에 지연
import된다.
"""
from __future__ import annotations
