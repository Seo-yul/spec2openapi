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

import sys
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
    SCHEMA_SELFCHECK,
    build_system,
    digest_operation,
    digest_schema,
    selfcheck_system,
    spec_outline,
    user_glossary,
    user_operation,
    user_schema,
    user_toolcheck,
)
from .providers import ProviderError, Suggestion
from .targets import (
    KINDS,
    Target,
    escape_token,
    find_targets,
    low_value_reason,
)


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


def plan(spec: dict, *, kinds: Iterable[str] = KINDS,
        policy: str = "default") -> tuple[dict, list[Target]]:
    """LLM 호출 없이 실제 실행이 볼 것과 같은 대상 목록을 계산한다.

    agentize_spec()이 실제로 쓰는 것과 같은 minify_for_mcp() 호출과
    already_generated() 차감을 거친다 - --dry-run과 사전 안내가 실제
    실행과 다른 숫자를 보여주면(Ruling 55) 사용자가 낸 판단이 틀린
    전제 위에 서게 된다.
    """
    working = minify_for_mcp(spec, enrich=("errors", "examples"))
    done = already_generated(working)
    targets = [t for t in find_targets(working, kinds=kinds, policy=policy)
              if t.pointer not in done]
    return working, targets


def call_estimate(targets: Iterable[Target], *, tool_count: int = 0
                  ) -> tuple[int, int, int]:
    """(대상 있는 스키마 수, desc/param 대상 있는 operation 수, 예상 호출 총수).

    pass 2a는 스키마당 1회, pass 2b는 desc나 param 대상이 있는
    operation당 1회 부른다 - 이 함수의 계산은 그 두 루프가 실제로 도는
    횟수와 정확히 같아야 한다(Ruling 57). targets가 비어 있으면
    pass 1(용어집)조차 부르지 않는다.

    tool_count는 --self-check 를 켰을 때 pass 3가 도는 횟수(tool 하나당
    1회)다. 채울 것이 없어 pass 1/2가 아예 안 돌아도 self-check 는
    돈다(Ruling 43) - 그래서 이것을 빼면 사전 출력이 0을 찍고 실제로는
    호출이 나가는, 비용을 좌우하지 않는 숫자가 된다.
    """
    targets = list(targets)
    if not targets:
        return 0, 0, tool_count
    schemas = len(_schema_targets(targets))
    op_bases = {t.pointer.rsplit("/", 1)[0] for t in targets if t.kind == "desc"}
    op_bases |= set(_param_pointers(targets))
    ops = len(op_bases)
    return schemas, ops, 1 + schemas + ops + tool_count


def _tool_payloads(spec: dict) -> tuple[list[dict] | None, str]:
    """FastMCP 가 실제로 보낼 tool 목록과, 만들지 못했을 때의 사유.

    성공하면 (목록, "") 을, 실패하면 (None, 사유) 를 돌려준다. 빈 목록과
    None 을 구분해야 한다 - 빈 목록은 "검사했고 문제가 없다"이지만 None 은
    "검사하지 못했다"이다. 사유를 함께 돌려주는 이유는 사유가 하나가
    아니기 때문이다: extra 부재, 실행 중인 이벤트 루프, round-trip 실패는
    사용자가 취할 조치가 각각 다르다.
    """
    try:
        import asyncio

        import httpx
        from fastmcp import Client, FastMCP
    except ImportError:
        from ..errors import MCP_HINT

        return None, f"MCP 런타임 extra 가 필요하다 ({MCP_HINT})"

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # checks.py 의 _run_fastmcp_roundtrip 과 같은 방어다: 이미 도는
        # 루프 안에서는 두 번째 루프를 시작할 수 없다. 이 가드가 없으면
        # 크래시가 "extra 부재"로 오보된다.
        return None, ("실행 중인 이벤트 루프 안에서는 FastMCP round-trip 을 "
                      "할 수 없다; 동기 컨텍스트에서 호출하라")

    # client 생성 자체를 try 안에 둔다 (Ruling 63) - httpx.AsyncClient()는
    # 잘못된 proxy 환경변수 등으로 생성 시점에 실패할 수 있고, 이 함수
    # 바깥의 어떤 호출자도 일반 Exception을 잡지 않는다
    # (cmd_agentize는 AgentizeError만, main()은 ValueError/OSError만) -
    # 밖에 두면 완료된 유료 보강 전체를 트레이스백으로 날린다.
    client = None
    try:
        client = httpx.AsyncClient(base_url="http://spec2openapi.invalid")

        async def _list():
            try:
                mcp = FastMCP.from_openapi(spec, client=client)
                async with Client(mcp) as c:
                    return [{"name": t.name, "description": t.description,
                             "inputSchema": t.inputSchema}
                            for t in await c.list_tools()]
            finally:
                await client.aclose()

        return asyncio.run(_list()), ""
    except Exception as exc:
        # _list() 가 시작되기 전에 실패했다면 finally 가 안 돌아 client 가
        # 열린 채로 남는다. checks.py 와 같은 best-effort 정리다. client
        # 생성 자체가 실패했으면 client는 여전히 None이라 정리할 것이
        # 없다.
        if client is not None and not client.is_closed:
            try:
                asyncio.run(client.aclose())
            except Exception:
                pass
        return None, f"FastMCP round-trip 실패: {exc}"


def run_self_check(spec: dict, provider: Any) -> list[str]:
    """tool payload만 보여주고 호출 가능성을 묻는다. 스펙은 수정하지 않는다."""
    tools, reason = _tool_payloads(spec)
    if tools is None:
        return [f"self-check 를 수행하지 못했다: {reason}"]

    notes: list[str] = []
    system = selfcheck_system()
    for tool in tools:
        try:
            reply = provider.complete(system, user_toolcheck(tool),
                                      SCHEMA_SELFCHECK)
        except ProviderError as exc:
            # pass 1/2 와 같은 폭이어야 한다. Exception 을 통째로
            # 삼키면 우리 코드의 버그가 'tool 이 모호하다'는 가짜
            # 경고로 둔갑해 진단이 불가능해진다.
            notes.append(f"{tool['name']}: self-check 실패 ({exc})")
            continue
        if not reply.get("callable"):
            notes.append(f"{tool['name']}: "
                         f"{reply.get('problem') or '모호함'}")
    return notes


def agentize_spec(spec: dict, provider: Any, *, kinds: Iterable[str] = KINDS,
                  policy: str = "default", allow_speculative: bool = False,
                  rename_tools: bool = False, self_check: bool = False,
                  max_ops: int | None = None,
                  language: str = "the language already used in the spec, "
                                  "or English if none") -> AgentizeResult:
    """스펙을 보강한 새 스펙과 보고서를 돌려준다. 입력은 변경하지 않는다."""
    # kinds 를 즉시 튜플로 고정한다 (Ruling 69) - 아래에서 find_targets()
    # 에 한 번, record_provenance() 에 또 한 번 넘긴다. generator 였다면
    # 첫 소비에서 소진되어 provenance 에 targets: [] 가 기록된다.
    kinds = tuple(kinds)
    if not isinstance(spec, dict):
        raise AgentizeError("agentize_spec expects an OpenAPI 3.x mapping, "
                            f"got {type(spec).__name__}")
    if not can_record(spec):
        raise AgentizeError(
            "root x-s2o가 매핑이 아니어서 provenance를 기록할 수 없다; "
            "기록 없이 보강하면 재실행이 중복 생성을 일으키므로 중단한다")

    working, targets = plan(spec, kinds=kinds, policy=policy)

    op_count = len(list(_operations(working)))
    if max_ops is not None and op_count > max_ops:
        raise AgentizeError(
            f"--max-ops {max_ops} 초과: 이 스펙은 operation이 {op_count}개다")

    failures: list[str] = []

    def _finish(spec_out: dict, rep: ApplyReport,
               tgts: list[Target]) -> AgentizeResult:
        # self-check 는 이 실행이 무엇을 썼는지와 무관하게 "이 tool 을
        # 호출할 수 있는가"를 묻는다. 채울 것이 없었던 실행에서도
        # 사용자가 답을 원할 수 있으므로 모든 경로에서 실행한다.
        checks = run_self_check(spec_out, provider) if self_check else None
        return AgentizeResult(spec=spec_out, report=rep, targets=tgts,
                              failures=failures, self_check=checks)

    if not targets:
        return _finish(working, ApplyReport(), [])

    outline = spec_outline(working)

    # --- pass 1: 용어집 ---
    # 진행 상황을 stderr 로 찍는다 (Ruling 59) - --concurrency 없이
    # 순차 실행이므로, 스키마·operation 이 많은 스펙에서는 이 세 줄이
    # 유일한 생존 신호다.
    glossary: dict | None = None
    try:
        glossary = provider.complete(
            build_system(None, language, rename_tools=rename_tools),
            user_glossary(outline), SCHEMA_GLOSSARY)
    except ProviderError as exc:  # 용어집 실패는 치명적이지 않다
        failures.append(f"pass 1 용어집: {exc}")
        print(f"pass 1  용어집 생성 ... 실패 ({exc})", file=sys.stderr)
    else:
        domain = glossary.get("domain") if isinstance(glossary, dict) else None
        n_terms = len((glossary or {}).get("glossary") or {})
        print(f"pass 1  용어집 생성 ... ok (domain={domain!r}, "
              f"용어 {n_terms}개)", file=sys.stderr)

    system = build_system(glossary, language, rename_tools=rename_tools)
    suggestions: list[Suggestion] = []

    # --- pass 2a: 공유 스키마 (operation보다 먼저) ---
    schemas = _component_schemas(working) or {}
    schema_targets = _schema_targets(targets)
    n_2a_ok = n_2a_fail = 0
    for sname, wanted in schema_targets.items():
        node = schemas.get(sname)
        if not isinstance(node, dict):
            continue
        try:
            reply = provider.complete(
                system, user_schema(sname, digest_schema(node, working),
                                    sorted(wanted)), SCHEMA_FIELDS)
        except ProviderError as exc:
            failures.append(f"pass 2a 스키마 {sname}: {exc}")
            n_2a_fail += 1
            continue
        n_2a_ok += 1
        # 요청하지 않은 필드는 무시한다. provider 가 fields_needed 를
        # 지킬 것이라 신뢰하지 않는다 - 여분 필드를 받아들이면 이미
        # 채워진 설명을 덮어쓰고 재실행이 no-op 이 아니게 된다.
        asked = set(wanted)
        for body in (reply.get("fields") or []):
            if not isinstance(body, dict):
                continue
            fname = body.get("name")
            if fname not in asked:
                continue
            suggestions.append(Suggestion(
                pointer=(f"#/components/schemas/{escape_token(sname)}"
                         f"/properties/{escape_token(fname)}"
                         "/description"),
                description=body.get("description"),
                grounding=str(body.get("grounding")),
                # "examples" 는 find_targets() 의 생산 대상이 아니라
                # example 적용 여부를 여는 permission 이다 (Ruling 54) -
                # kinds 에 없으면 example 을 아예 만들지 않는다.
                example=body.get("example") if "examples" in kinds else None))
    if schema_targets:
        tail = f", {n_2a_fail} 실패" if n_2a_fail else ""
        print(f"pass 2a 공유 스키마 {n_2a_ok + n_2a_fail}개 ... "
              f"{n_2a_ok} ok{tail}", file=sys.stderr)

    # --- pass 2b: operation ---
    op_ptrs = {t.pointer.rsplit("/", 1)[0] for t in targets
               if t.kind == "desc"}
    param_ptrs = _param_pointers(targets)
    op_schema = SCHEMA_OPERATION_RENAME if rename_tools else SCHEMA_OPERATION
    n_2b_ok = n_2b_fail = 0
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
        except ProviderError as exc:
            failures.append(f"pass 2b operation {op.get('operationId')}: {exc}")
            n_2b_fail += 1
            continue
        n_2b_ok += 1
        grounding = str(reply.get("grounding"))
        if base in op_ptrs:
            # 파라미터 때문에 이 패스에 들어온 operation 은 desc 대상이
            # 아니다. 요청하지 않은 필드는 쓰지 않는다 (Ruling 25 와 같은
            # 원칙). SCHEMA_OPERATION 이 description 을 필수로 요구하므로
            # 모델은 항상 값을 주지만, 우리가 받아들일지는 별개다.
            if reply.get("description"):
                suggestions.append(Suggestion(f"{base}/description",
                                              reply["description"], grounding))
            # summary 는 대상으로 지목되는 일이 없다(find_targets 는
            # /description 만 낸다). description 이 저품질이라는
            # 이유로 멀쩡한 summary 까지 덮어쓰면 사용자가 쓴 글이
            # --overwrite 없이 소리 없이 사라진다.
            if reply.get("summary") and low_value_reason(
                    op.get("summary"), name=op.get("operationId")):
                suggestions.append(Suggestion(f"{base}/summary",
                                              reply["summary"], grounding))
        if rename_tools and reply.get("operationId"):
            suggestions.append(Suggestion(f"{base}/operationId",
                                          reply["operationId"], grounding))
        # Ruling 25 와 같은 원칙: 요청한 이름만 받아들인다.
        for body in (reply.get("parameters") or []):
            if not isinstance(body, dict):
                continue
            ptr = (param_ptrs.get(base) or {}).get(body.get("name"))
            if not ptr:
                continue
            suggestions.append(Suggestion(
                ptr, body.get("description"), str(body.get("grounding"))))
    if op_ptrs or param_ptrs:
        tail = f", {n_2b_fail} 실패" if n_2b_fail else ""
        print(f"pass 2b operation {n_2b_ok + n_2b_fail}개 ... "
              f"{n_2b_ok} ok{tail}", file=sys.stderr)

    out, report = apply_suggestions(
        working, suggestions, allow_speculative=allow_speculative,
        rename_tools=rename_tools)

    if not report.applied and not report.renamed:
        # 문서에 아무것도 새로 쓰지 않았다. minify_for_mcp 와 같은 계약을
        # 따른다: 아무것도 바꾸지 않은 실행은 동일한 문서를 반환한다.
        # targets 가 애초에 없을 때의 조기 반환과 같은 원칙이며, 문서가
        # 그대로이므로 verify 판정도 입력과 같다.
        return _finish(working, report, targets)

    verdict = verify(out)
    if not verdict.ok:
        problems = "; ".join(r.message for r in verdict.results
                             if r.status == "fail")
        raise AgentizeError(f"보강 결과가 verify를 통과하지 못해 전체를 "
                            f"취소한다: {problems}")

    record_provenance(out, report, provider=getattr(provider, "name", "?"),
                      model=getattr(provider, "model", "?"), targets=kinds)
    return _finish(out, report, targets)
