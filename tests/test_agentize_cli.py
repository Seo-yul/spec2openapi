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
