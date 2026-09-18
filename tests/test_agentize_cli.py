"""agentize CLI: 플래그 검증, dry-run, 종료 코드, 포맷 추론."""
from __future__ import annotations

import json
from pathlib import Path

from spec2openapi.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_mutually_exclusive_policy_flags_exit_2(capsys):
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "--keep-low-value", "--overwrite"])
    assert rc == 2
    assert "상호 배타" in capsys.readouterr().err


def test_dry_run_makes_no_provider_call_and_writes_no_file(tmp_path, capsys):
    out = tmp_path / "out.yaml"
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "-o", str(out), "--dry-run"])
    assert rc == 0
    assert not out.exists()
    printed = capsys.readouterr().out
    assert "보강 대상" in printed


def test_dry_run_reports_targets_by_kind(capsys):
    main(["agentize", str(EXAMPLES / "petstore-upgraded.openapi.yaml"),
          "--dry-run"])
    printed = capsys.readouterr().out
    assert "properties" in printed


def test_missing_provider_dependency_exits_2(monkeypatch, capsys):
    """provider 어댑터가 의존성 부족으로 ImportError 를 내면 CLI 는
    종료 코드 2 와 함께 그 메시지를 보여준다. 어댑터가 그 메시지를
    만드는지는 provider 태스크의 계약이며 여기서 검사하지 않는다."""
    def boom(args):
        raise ImportError(
            "agentize --provider anthropic requires the SDK; install it "
            "with: pip install 'spec2openapi[llm-anthropic]'")

    monkeypatch.setattr("spec2openapi.cli._resolve_provider", boom)
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "--provider", "anthropic", "-o", "/dev/null"])
    assert rc == 2
    assert "llm-anthropic" in capsys.readouterr().err


def test_unknown_target_kind_exits_2(capsys):
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "--target", "nonsense", "--dry-run"])
    assert rc == 2
    assert "nonsense" in capsys.readouterr().err


def test_json_output_format_is_inferred_from_extension(tmp_path, monkeypatch):
    from spec2openapi.agentize import AgentizeResult
    from spec2openapi.agentize.apply import ApplyReport

    spec_out = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                "paths": {}}

    class _Stub:
        name = "stub"
        model = "stub-1"

    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: _Stub())
    monkeypatch.setattr(
        "spec2openapi.cli._agentize_run",
        lambda *a, **k: AgentizeResult(spec=spec_out, report=ApplyReport()))
    out = tmp_path / "spec.json"
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "-o", str(out), "--provider", "anthropic"])
    assert rc == 0
    assert json.loads(out.read_text())["openapi"] == "3.0.3"


def test_max_ops_exceeded_exits_2_before_resolving_a_provider(monkeypatch,
                                                              capsys):
    """--max-ops 초과는 사용자 입력 오류(2)이지 verify 실패(1)가 아니다.
    그리고 provider 를 만들기 전에 막아야 비용이 0 이다."""
    called = []
    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: called.append(1))
    rc = main(["agentize", str(EXAMPLES / "petstore-upgraded.openapi.yaml"),
               "--max-ops", "0", "-o", "/dev/null"])
    assert rc == 2
    assert called == []
    assert "--max-ops" in capsys.readouterr().err


def test_preflight_counts_operations_not_paths(monkeypatch, capsys):
    """petstore-upgraded 는 경로 3개에 operation 5개다. 찍히는 숫자는
    비용을 좌우하는 숫자여야 한다."""
    class _Stub:
        name = "stub"
        model = "stub-1"

    from spec2openapi.agentize import AgentizeResult
    from spec2openapi.agentize.apply import ApplyReport

    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: _Stub())
    monkeypatch.setattr(
        "spec2openapi.cli._agentize_run",
        lambda *a, **k: AgentizeResult(
            spec={"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                  "paths": {}},
            report=ApplyReport()))
    main(["agentize", str(EXAMPLES / "petstore-upgraded.openapi.yaml"),
          "-o", "/dev/null"])
    assert "operations=5" in capsys.readouterr().err


def test_verify_failure_exits_1(monkeypatch, capsys):
    """verify 실패로 인한 전체 취소는 1 이다 - 2 와 구분된다."""
    from spec2openapi.agentize import AgentizeError

    class _Stub:
        name = "stub"
        model = "stub-1"

    def boom(*a, **k):
        raise AgentizeError("보강 결과가 verify를 통과하지 못해 전체를 취소한다")

    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: _Stub())
    monkeypatch.setattr("spec2openapi.cli._agentize_run", boom)
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "-o", "/dev/null"])
    assert rc == 1
    assert "verify" in capsys.readouterr().err


# --- Ruling 55/57: dry-run/사전 안내 숫자 정합성 -----------------------------

def test_dry_run_subtracts_already_generated_pointers(tmp_path, capsys):
    """dry-run은 실제 실행(agentize_spec)이 보는 것과 같은 대상 목록을
    봐야 한다 - minify_for_mcp를 거치고 이미 채운 포인터
    (x-s2o.agentize.generated)를 뺀 뒤 세야 한다 (Ruling 55). 여기서는
    'name'을 이미 채운 것으로 기록해 둔 스펙을 dry-run 하면 'name'은
    대상에서 빠져야 함을 확인한다."""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("""\
openapi: 3.0.3
info: {title: t, version: '1'}
paths:
  /pets:
    post:
      operationId: createPet
      responses: {'200': {description: ok}}
components:
  schemas:
    Pet:
      type: object
      properties:
        name: {type: string}
        tag: {type: string}
x-s2o:
  agentize:
    provider: fake
    model: fake-1
    targets: [properties]
    generated:
      "#/components/schemas/Pet/properties/name/description":
        {grounding: named, provider: fake, model: fake-1}
    dropped_speculative: 0
""", encoding="utf-8")
    rc = main(["agentize", str(spec_path), "--target", "properties",
               "--dry-run"])
    assert rc == 0
    printed = capsys.readouterr().out
    # name은 already_generated로 빠지고 tag만 남아야 한다
    assert "보강 대상 1건" in printed


def test_preflight_and_dry_run_report_the_same_schema_and_call_counts(
        monkeypatch, capsys):
    """사전 안내(provider=... 줄)와 --dry-run은 같은 schemas/calls 수를
    보여줘야 한다 - 둘 다 plan()/call_estimate()를 공유하기 때문이다
    (Ruling 57)."""
    import re

    from spec2openapi.agentize import AgentizeResult
    from spec2openapi.agentize.apply import ApplyReport

    spec_path = str(EXAMPLES / "petstore-upgraded.openapi.yaml")

    main(["agentize", spec_path, "--dry-run"])
    dry_out = capsys.readouterr().out
    m = re.search(r"대상 스키마 (\d+)건, 예상 LLM 호출 (\d+)회", dry_out)
    assert m, dry_out
    dry_schemas, dry_calls = m.group(1), m.group(2)
    assert int(dry_calls) > 0

    class _Stub:
        name = "stub"
        model = "stub-1"

    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: _Stub())
    monkeypatch.setattr(
        "spec2openapi.cli._agentize_run",
        lambda *a, **k: AgentizeResult(
            spec={"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                  "paths": {}},
            report=ApplyReport()))
    main(["agentize", spec_path, "-o", "/dev/null"])
    err = capsys.readouterr().err
    assert f"schemas={dry_schemas}" in err
    assert f"calls={dry_calls}" in err


# --- Ruling 56: 전면 실패는 산출물을 내지 않는다 -----------------------------

def test_total_provider_failure_writes_nothing_and_exits_2(
        monkeypatch, tmp_path, capsys):
    from spec2openapi.agentize import AgentizeResult
    from spec2openapi.agentize.apply import ApplyReport

    class _Stub:
        name = "stub"
        model = "stub-1"

    out = tmp_path / "out.yaml"
    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: _Stub())
    monkeypatch.setattr(
        "spec2openapi.cli._agentize_run",
        lambda *a, **k: AgentizeResult(
            spec={"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                  "paths": {}},
            report=ApplyReport(),
            failures=["pass 1 용어집: 모의 실패", "pass 2b operation x: 모의 실패"]))
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "-o", str(out)])
    assert rc == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "모든 LLM 호출이 실패" in err


def test_partial_failure_still_writes_and_exits_0(monkeypatch, tmp_path,
                                                  capsys):
    """일부만 실패했으면 - 성공한 게 있으면 - 부분 실패 격리를 유지해
    산출물을 쓰고 0으로 끝난다. 전부 실패했을 때만 2다 (Ruling 56)."""
    from spec2openapi.agentize import AgentizeResult
    from spec2openapi.agentize.apply import ApplyReport

    class _Stub:
        name = "stub"
        model = "stub-1"

    out = tmp_path / "out.yaml"
    report = ApplyReport(applied={
        "#/components/schemas/Pet/properties/name/description": "named"})
    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: _Stub())
    monkeypatch.setattr(
        "spec2openapi.cli._agentize_run",
        lambda *a, **k: AgentizeResult(
            spec={"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
                  "paths": {}},
            report=report,
            failures=["pass 2b operation x: 모의 실패"]))
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "-o", str(out)])
    assert rc == 0
    assert out.exists()


def test_provider_construction_failure_gets_a_clear_hint_and_exits_2(
        monkeypatch, capsys):
    """anthropic.Anthropic()이 생성 시점에 예외를 던지면(자격 증명도
    프로필도 없을 때) main()을 트레이스백 없이 종료 코드 2로 지나가야
    한다 (Ruling 56)."""
    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("모의: 자격 증명 없음")

    monkeypatch.setattr(
        "spec2openapi.agentize.anthropic_provider.AnthropicProvider", _Boom)
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "--provider", "anthropic", "-o", "/dev/null"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ant auth login" in err and "ANTHROPIC_API_KEY" in err


# --- Ruling 61: --rename-tools 결과 보고 -------------------------------------

def test_rename_tools_output_is_reported(monkeypatch, capsys):
    from spec2openapi.agentize import AgentizeResult
    from spec2openapi.agentize.apply import ApplyReport

    class _Stub:
        name = "stub"
        model = "stub-1"

    spec_out = {
        "openapi": "3.0.3", "info": {"title": "t", "version": "1"},
        "paths": {"/pets": {"post": {
            "operationId": "create_pet",
            "responses": {"200": {"description": "ok"}}}}}}
    report = ApplyReport(renamed={"#/paths/~1pets/post": "createPet"})
    monkeypatch.setattr("spec2openapi.cli._resolve_provider",
                        lambda args: _Stub())
    monkeypatch.setattr(
        "spec2openapi.cli._agentize_run",
        lambda *a, **k: AgentizeResult(spec=spec_out, report=report))
    rc = main(["agentize", str(EXAMPLES / "orders.openapi.yaml"),
               "--rename-tools", "-o", "/dev/null"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "createPet" in err and "create_pet" in err
