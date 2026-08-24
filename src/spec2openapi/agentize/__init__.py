"""LLM 기반 agent-facing tool 표면 보강 (선택 기능).

import 시점에 어떤 LLM SDK도 끌어오지 않는다 - provider 어댑터는 실제
사용 시점에 지연 import된다.

파이프라인:
    minify_for_mcp(결정론적 선보강)
      -> find_targets(결정론적 대상 선정)
      -> pass 1  서비스 개요 + 용어집        [LLM] 1회
      -> pass 2a 공유 스키마 property        [LLM] 스키마당 1회
      -> pass 2b operation summary/desc      [LLM] op당 1회
      -> apply(화이트리스트) -> verify(계약) -> provenance
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..checks import _component_schemas, verify
from ..errors import ConversionError
from ..minify import minify_for_mcp
from ..openapi import _operations, _unescape_pointer_token
from .apply import (
    ApplyReport,
    already_generated,
    apply_suggestions,
    can_record,
    record_provenance,
)
from .prompts import (
    SCHEMA_FIELDS,
    SCHEMA_GLOSSARY,
    SCHEMA_OPERATION,
    SCHEMA_OPERATION_RENAME,
    build_system,
    digest_operation,
    digest_schema,
    spec_outline,
    user_glossary,
    user_operation,
    user_schema,
)
from .providers import Suggestion
from .targets import KINDS, Target, escape_token, find_targets


class AgentizeError(ConversionError):
    """보강을 진행할 수 없거나 결과가 계약을 깼다."""


@dataclass
class AgentizeResult:
    spec: dict
    report: ApplyReport
    targets: list[Target] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    self_check: list[str] | None = None


def _schema_targets(targets: Iterable[Target]) -> dict[str, list[str]]:
    """스키마 이름 -> 채워야 할 최상위 property 이름 목록."""
    out: dict[str, list[str]] = {}
    for t in targets:
        if t.kind != "properties":
            continue
        parts = t.pointer.split("/")
        # #/components/schemas/<S>/properties/<P>/description
        if len(parts) < 7 or parts[1] != "components":
            continue
        out.setdefault(_unescape_pointer_token(parts[3]), []).append(t.name)
    return out


def _param_pointers(targets: Iterable[Target]) -> dict[str, dict[str, str]]:
    """operation base -> {parameter 이름: 포인터}.

    targets 가 이미 원본 리스트 인덱스를 담은 포인터를 갖고 있으므로
    인덱스를 다시 계산하지 않는다 - digest_operation 은 $ref 파라미터를
    건너뛰어 인덱스가 어긋난다. 한 operation 안에서 이름이 겹치면 어느
    파라미터인지 모호하므로 그 이름은 아예 제외한다 - 엉뚱한 파라미터에
    설명을 쓰는 것보다 비워두는 편이 낫다.
    """
    out: dict[str, dict[str, str]] = {}
    seen: dict[str, set[str]] = {}
    for t in targets:
        if t.kind != "params":
            continue
        base = t.pointer.rsplit("/parameters/", 1)[0]
        names = seen.setdefault(base, set())
        slot = out.setdefault(base, {})
        if t.name in names:
            slot.pop(t.name, None)
        else:
            names.add(t.name)
            slot[t.name] = t.pointer
    # 중복 이름 배제로 비게 된 항목은 내보내지 않는다 - 존재하지만 빈
    # 항목은 게이트를 통과시켜 채울 것 없는 호출을 낭비한다.
    return {base: names for base, names in out.items() if names}


def agentize_spec(spec: dict, provider: Any, *, kinds: Iterable[str] = KINDS,
                  policy: str = "default", allow_speculative: bool = False,
                  rename_tools: bool = False, self_check: bool = False,
                  max_ops: int | None = None,
                  language: str = "the language already used in the spec, "
                                  "or English if none") -> AgentizeResult:
    """스펙을 보강한 새 스펙과 보고서를 돌려준다. 입력은 변경하지 않는다."""
    if not isinstance(spec, dict):
        raise AgentizeError("agentize_spec expects an OpenAPI 3.x mapping, "
                            f"got {type(spec).__name__}")
    if not can_record(spec):
        raise AgentizeError(
            "root x-s2o가 매핑이 아니어서 provenance를 기록할 수 없다; "
            "기록 없이 보강하면 재실행이 중복 생성을 일으키므로 중단한다")

    working = minify_for_mcp(spec, enrich=("errors", "examples"))
    done = already_generated(working)
    targets = [t for t in find_targets(working, kinds=kinds, policy=policy)
               if t.pointer not in done]

    op_count = len(list(_operations(working)))
    if max_ops is not None and op_count > max_ops:
        raise AgentizeError(
            f"--max-ops {max_ops} 초과: 이 스펙은 operation이 {op_count}개다")

    if not targets:
        return AgentizeResult(spec=working, report=ApplyReport(), targets=[])

    failures: list[str] = []
    outline = spec_outline(working)

    # --- pass 1: 용어집 ---
    glossary: dict | None = None
    try:
        glossary = provider.complete(
            build_system(outline, None, language),
            user_glossary(outline), SCHEMA_GLOSSARY)
    except Exception as exc:  # 용어집 실패는 치명적이지 않다
        failures.append(f"pass 1 용어집: {exc}")

    system = build_system(outline, glossary, language)
    suggestions: list[Suggestion] = []

    # --- pass 2a: 공유 스키마 (operation보다 먼저) ---
    schemas = _component_schemas(working) or {}
    for sname, wanted in _schema_targets(targets).items():
        node = schemas.get(sname)
        if not isinstance(node, dict):
            continue
        try:
            reply = provider.complete(
                system, user_schema(sname, digest_schema(node, working),
                                    sorted(wanted)), SCHEMA_FIELDS)
        except Exception as exc:
            failures.append(f"pass 2a 스키마 {sname}: {exc}")
            continue
        # 요청하지 않은 필드는 무시한다. provider 가 fields_needed 를
        # 지킬 것이라 신뢰하지 않는다 - 여분 필드를 받아들이면 이미
        # 채워진 설명을 덮어쓰고 재실행이 no-op 이 아니게 된다.
        asked = set(wanted)
        for fname, body in (reply.get("fields") or {}).items():
            if fname not in asked or not isinstance(body, dict):
                continue
            suggestions.append(Suggestion(
                pointer=(f"#/components/schemas/{escape_token(sname)}"
                         f"/properties/{escape_token(fname)}"
                         "/description"),
                description=body.get("description"),
                grounding=str(body.get("grounding")),
                example=body.get("example")))

    # --- pass 2b: operation ---
    op_ptrs = {t.pointer.rsplit("/", 1)[0] for t in targets
               if t.kind == "desc"}
    param_ptrs = _param_pointers(targets)
    op_schema = SCHEMA_OPERATION_RENAME if rename_tools else SCHEMA_OPERATION
    for path, method, op in _operations(working):
        base = f"#/paths/{escape_token(path)}/{method}"
        # desc target 이 없어도 이 operation의 parameter target 이 있으면
        # 여전히 질의해야 한다 - 그렇지 않으면 params kind는 절대 채워지지
        # 않는다.
        if base not in op_ptrs and base not in param_ptrs:
            continue
        digested = digest_operation(path, method, op, working)
        wanted_params = sorted((param_ptrs.get(base) or {}))
        if wanted_params:
            digested["parameters_needed"] = wanted_params
        try:
            reply = provider.complete(system, user_operation(digested),
                                      op_schema)
        except Exception as exc:
            failures.append(f"pass 2b operation {op.get('operationId')}: {exc}")
            continue
        grounding = str(reply.get("grounding"))
        if base in op_ptrs:
            # 파라미터 때문에 이 패스에 들어온 operation 은 desc 대상이
            # 아니다. 요청하지 않은 필드는 쓰지 않는다 (Ruling 25 와 같은
            # 원칙). SCHEMA_OPERATION 이 description 을 필수로 요구하므로
            # 모델은 항상 값을 주지만, 우리가 받아들일지는 별개다.
            if reply.get("description"):
                suggestions.append(Suggestion(f"{base}/description",
                                              reply["description"], grounding))
            if reply.get("summary"):
                suggestions.append(Suggestion(f"{base}/summary",
                                              reply["summary"], grounding))
        if rename_tools and reply.get("operationId"):
            suggestions.append(Suggestion(f"{base}/operationId",
                                          reply["operationId"], grounding))
        # Ruling 25 와 같은 원칙: 요청한 이름만 받아들인다.
        for pname, body in (reply.get("parameters") or {}).items():
            ptr = (param_ptrs.get(base) or {}).get(pname)
            if not ptr or not isinstance(body, dict):
                continue
            suggestions.append(Suggestion(
                ptr, body.get("description"), str(body.get("grounding"))))

    out, report = apply_suggestions(
        working, suggestions, allow_speculative=allow_speculative,
        rename_tools=rename_tools)

    if not report.applied and not report.renamed:
        # 문서에 아무것도 새로 쓰지 않았다. minify_for_mcp 와 같은 계약을
        # 따른다: 아무것도 바꾸지 않은 실행은 동일한 문서를 반환한다.
        # targets 가 애초에 없을 때의 조기 반환과 같은 원칙이며, 문서가
        # 그대로이므로 verify 판정도 입력과 같다.
        return AgentizeResult(spec=working, report=report, targets=targets,
                              failures=failures)

    verdict = verify(out)
    if not verdict.ok:
        problems = "; ".join(r.message for r in verdict.results
                             if r.status == "fail")
        raise AgentizeError(f"보강 결과가 verify를 통과하지 못해 전체를 "
                            f"취소한다: {problems}")

    record_provenance(out, report, provider=getattr(provider, "name", "?"),
                      model=getattr(provider, "model", "?"), targets=kinds)
    return AgentizeResult(spec=out, report=report, targets=targets,
                          failures=failures)
