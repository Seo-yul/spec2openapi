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
