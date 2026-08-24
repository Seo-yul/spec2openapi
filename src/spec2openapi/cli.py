"""spec2openapi command line interface.

  spec2openapi convert service.wsdl -o service.openapi.yaml
  spec2openapi upgrade swagger2.json -o service.openapi.yaml
  spec2openapi inspect service.wsdl
  spec2openapi validate service.openapi.yaml   # incl. FastMCP round-trip
  spec2openapi serve spec.yaml --transport http   # reference MCP runtime
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .errors import MCP_HINT

_MCP_HINT = f"install the MCP runtime extras first: {MCP_HINT}"


def _is_wsdl_source(src: str) -> bool:
    low = src.lower()
    if low.endswith((".yaml", ".yml", ".json")):
        return False
    if low.endswith((".wsdl", "?wsdl")):
        return True
    if "?" in low and "wsdl" in low.split("?", 1)[1]:  # e.g. ?singleWsdl
        return True
    p = Path(src)
    if p.exists():
        head = p.read_text(encoding="utf-8-sig", errors="replace")[:512].lstrip()
        return head.startswith("<")
    # a remote URL without a spec extension: zeep can fetch WSDLs, and
    # load_spec cannot read a URL, so treat it as WSDL
    if low.startswith(("http://", "https://")):
        return True
    return False


def _load_or_convert(source: str) -> dict:
    from .convert import convert_wsdl, load_spec
    from .swagger import convert_swagger, is_swagger2

    if _is_wsdl_source(source):
        return convert_wsdl(source)
    spec = load_spec(source)
    if is_swagger2(spec):
        print("note: Swagger 2.0 input detected; upgrading to OpenAPI 3.0 "
              "in memory (see `spec2openapi upgrade`)", file=sys.stderr)
        spec = convert_swagger(spec)
    return spec


def _add_bridge_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--endpoint", help="override SOAP endpoint URL "
                                      "(env SPEC2OPENAPI_ENDPOINT)")
    p.add_argument("--auth", choices=["basic", "wsse"],
                   help="authentication scheme (env SPEC2OPENAPI_AUTH)")
    p.add_argument("--username", help="env SPEC2OPENAPI_USERNAME")
    p.add_argument("--password", help="env SPEC2OPENAPI_PASSWORD")
    p.add_argument("--timeout", type=float, default=None,
                   help="SOAP call timeout seconds (default 30)")
    p.add_argument("--insecure", action="store_true",
                   help="disable TLS certificate verification")


def _bridge_options(args):
    from .bridge import BridgeOptions

    opt = BridgeOptions.from_env()
    if args.endpoint:
        opt.endpoint = args.endpoint
    if args.auth:
        opt.auth = args.auth
    if args.username:
        opt.username = args.username
    if args.password:
        opt.password = args.password
    if args.timeout is not None:
        opt.timeout = args.timeout
    if args.insecure:
        opt.verify = False
    return opt


def cmd_convert(args) -> int:
    from .convert import convert_wsdl, dump_spec

    src, content = args.wsdl, None
    if args.wsdl == "-":
        src, content = None, sys.stdin.buffer.read()
    spec = convert_wsdl(
        src, content=content,
        title=args.title, version=args.spec_version,
        base_path=args.base_path, service=args.service, port=args.port_name,
        prefer_soap12=args.prefer_soap12, strict=args.strict,
        openapi_version=args.openapi_version,
        forbid_external=args.forbid_external, huge_tree=args.huge_tree,
    )
    fmt = args.format or ("json" if (args.output or "").lower().endswith(".json") else "yaml")
    text = dump_spec(spec, fmt)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        n = len(spec.get("paths", {}))
        print(f"wrote {args.output} ({n} operations)", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_inspect(args) -> int:
    from .parser import parse_wsdl

    parsed = parse_wsdl(args.wsdl, forbid_external=args.forbid_external,
                        huge_tree=args.huge_tree)
    print(f"service : {parsed.name}")
    if parsed.documentation:
        print(f"doc     : {parsed.documentation}")
    print(f"ops     : {len(parsed.operations)}")
    for op in parsed.operations:
        params = ", ".join(n for n, _ in op.input_element.type.elements)
        extras = []
        if op.headers:
            extras.append(f"headers: {', '.join(h.part for h in op.headers)}")
        if op.faults:
            extras.append(f"faults: {', '.join(f.name for f in op.faults)}")
        suffix = f"  ({'; '.join(extras)})" if extras else ""
        print(f"  - {op.op_id}({params})  [SOAP {op.soap_version}, {op.style}]"
              f" {op.endpoint}{suffix}")
        if op.documentation:
            print(f"      {op.documentation}")
    for name, reason in parsed.skipped:
        print(f"  ! skipped {name}: {reason}")
    return 0


def cmd_upgrade(args) -> int:
    from .convert import dump_spec, load_spec
    from .swagger import convert_swagger

    spec = load_spec(args.source)
    upgraded = convert_swagger(spec, openapi_version=args.openapi_version,
                               strict=args.strict)

    report = upgraded.get("x-s2o", {})
    for kind in ("assumptions", "lossy"):
        for msg in report.get(kind, []):
            print(f"{kind[:-1] if kind.endswith('s') else kind}: {msg}",
                  file=sys.stderr)

    fmt = args.format or ("json" if (args.output or "").lower().endswith(".json") else "yaml")
    text = dump_spec(upgraded, fmt)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        n = len(upgraded.get("paths", {}))
        print(f"wrote {args.output} ({n} paths, "
              f"{len(report.get('assumptions', []))} assumptions, "
              f"{len(report.get('lossy', []))} lossy)", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_validate(args) -> int:
    """verify() as a CLI: static checks + optional deep validation."""
    from .checks import _component_schemas, verify
    from .openapi import _operations

    spec = _load_or_convert(args.source)
    report = verify(spec)

    if args.format == "json":
        import json

        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.ok else 1

    by_id = {}
    for r in report.results:
        by_id.setdefault(r.id, []).append(r)

    op_ids = [op.get("operationId")
              for _, _, op in _operations(spec) if op.get("operationId")]
    # components (or components.schemas) may be null (`components:` with
    # no value) rather than absent; _component_schemas tolerates both
    schemas = _component_schemas(spec) if isinstance(spec, dict) else {}
    print(f"operations        : {len(op_ids)}")
    print(f"component schemas : {len(schemas)}")

    osv = by_id.get("openapi.schema-valid", [None])[0]
    if osv is not None and osv.status == "pass":
        print("openapi-spec-validator: OK")
    elif osv is not None and osv.status == "skip":
        print("openapi-spec-validator: not installed (skipped)")

    rt = by_id.get("fastmcp.roundtrip", [None])[0]
    if rt is not None and rt.status == "pass":
        tools = (rt.data or {}).get("tools", [])
        print(f"FastMCP round-trip: OK ({len(tools)} tools)")
        for tool in tools:
            print(f"  - {tool['name']}({', '.join(tool['params'])})")
    elif rt is not None and rt.status == "skip":
        print(f"FastMCP round-trip: fastmcp not installed (skipped) "
              f"- {_MCP_HINT}")

    for r in report.results:
        if r.status == "warn":
            print(f"note: {r.message}")

    fails = [r for r in report.results if r.status == "fail"]
    if fails:
        print("\nFAIL")
        for r in fails:
            print(f"  ! {r.message}")
        return 1
    print("\nOK: spec is FastMCP-convertible")
    return 0


def cmd_serve(args) -> int:
    spec = _load_or_convert(args.source)
    try:
        # everything [mcp]-flavored lives inside the guard: server, the
        # bridge import in _bridge_options, and fastmcp's lazy imports
        from .server import from_openapi_spec

        mcp = from_openapi_spec(
            spec, options=_bridge_options(args),
            validate_output=args.validate_output,
        )
    except ImportError:
        print(f"error: {_MCP_HINT}", file=sys.stderr)
        return 2
    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port,
                path=args.path, show_banner=False)
    else:
        mcp.run(show_banner=False)  # stdio
    return 0


def _resolve_provider(args):
    """--provider / 환경변수 / 키 감지 순으로 provider를 만든다.

    키 존재를 미리 검사하지 않는다: Anthropic SDK는 `ant auth login`
    프로필로도 동작하므로, 키가 환경변수에 없다고 막으면 정상 사용자를
    막게 된다. 실패는 실제 호출 시점에 판단한다.
    """
    import os

    name = (args.provider or os.environ.get("SPEC2OPENAPI_LLM_PROVIDER") or "")
    if not name:
        if os.environ.get("ANTHROPIC_API_KEY"):
            name = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            name = "openai"
        else:
            name = "anthropic"  # 프로필 인증 가능성이 있으므로 기본값
    if name == "anthropic":
        from .agentize.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=args.model)
    if name == "openai":
        from .agentize.openai_provider import OpenAIProvider

        return OpenAIProvider(model=args.model)
    raise ValueError(f"알 수 없는 provider: {name!r}")


def _agentize_run(spec, provider, **kwargs):
    """테스트가 갈아끼울 수 있도록 분리한 실행 지점."""
    from .agentize import agentize_spec

    return agentize_spec(spec, provider, **kwargs)


def cmd_agentize(args) -> int:
    from .agentize import AgentizeError
    from .agentize.targets import KINDS, find_targets
    from .convert import dump_spec

    if args.keep_low_value and args.overwrite:
        print("error: --keep-low-value 와 --overwrite 는 상호 배타다",
              file=sys.stderr)
        return 2
    kinds = tuple(k.strip() for k in args.target.split(",") if k.strip())
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        print(f"error: 알 수 없는 --target 값 {unknown[0]!r}; "
              f"가능한 값: {', '.join(KINDS)}", file=sys.stderr)
        return 2
    policy = ("overwrite" if args.overwrite
              else "empty-only" if args.keep_low_value else "default")

    spec = _load_or_convert(args.source)

    if args.dry_run:
        targets = find_targets(spec, kinds=kinds, policy=policy)
        by_kind: dict[str, int] = {}
        for t in targets:
            by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
        print(f"보강 대상 {len(targets)}건")
        for kind in kinds:
            if by_kind.get(kind):
                print(f"  {kind:12s} {by_kind[kind]:5d}")
        if not targets:
            print("  (이 스펙은 손댈 곳이 없다)")
        return 0

    try:
        provider = _resolve_provider(args)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    n_ops = len(spec.get("paths", {}))
    print(f"provider={provider.name} model={provider.model} "
          f"operations={n_ops}", file=sys.stderr)

    try:
        result = _agentize_run(
            spec, provider, kinds=kinds, policy=policy,
            allow_speculative=args.allow_speculative,
            rename_tools=args.rename_tools, self_check=args.self_check,
            max_ops=args.max_ops, language=args.language)
    except AgentizeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for note in result.failures:
        print(f"warn: {note}", file=sys.stderr)
    rep = result.report
    print(f"applied {len(rep.applied)}건, "
          f"speculative {rep.dropped_speculative}건 폐기, "
          f"거부 {len(rep.rejected)}건", file=sys.stderr)
    if rep.dropped_speculative:
        print("      (--allow-speculative 로 유지할 수 있다)", file=sys.stderr)

    fmt = args.format or ("json" if (args.output or "").lower()
                          .endswith(".json") else "yaml")
    text = dump_spec(result.spec, fmt)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(
        prog="spec2openapi",
        description="Convert SOAP/WSDL services into FastMCP-ready OpenAPI "
                    "specs (x-soap extensions carry the SOAP binding).",
    )
    ap.add_argument("--version", action="version",
                    version=f"spec2openapi {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="WSDL -> OpenAPI spec with x-soap extensions")
    c.add_argument("wsdl", help="WSDL path, URL, zip bundle, or '-' for stdin")
    c.add_argument("-o", "--output", help="output file (default: stdout)")
    c.add_argument("--format", choices=["yaml", "json"])
    c.add_argument("--title", help="override info.title")
    c.add_argument("--spec-version", default="1.0.0", help="info.version")
    c.add_argument("--base-path", default="/operations")
    c.add_argument("--openapi-version", choices=["3.0", "3.1"], default="3.0")
    c.add_argument("--service", help="pick a wsdl:service by name")
    c.add_argument("--port-name", help="pick a wsdl:port by name")
    c.add_argument("--prefer-soap12", action="store_true")
    c.add_argument("--strict", action="store_true",
                   help="fail instead of skipping unsupported operations")
    c.add_argument("--forbid-external", action="store_true",
                   help="refuse to fetch remote wsdl:/xsd: imports "
                        "(recommended for WSDLs from untrusted sources)")
    c.add_argument("--huge-tree", action="store_true",
                   help="lift libxml2 depth/size limits for very large WSDLs")
    c.set_defaults(fn=cmd_convert)

    i = sub.add_parser("inspect", help="list operations found in a WSDL")
    i.add_argument("wsdl", help="WSDL path or URL")
    i.add_argument("--forbid-external", action="store_true",
                   help="refuse to fetch remote wsdl:/xsd: imports")
    i.add_argument("--huge-tree", action="store_true",
                   help="lift libxml2 depth/size limits for very large WSDLs")
    i.set_defaults(fn=cmd_inspect)

    u = sub.add_parser("upgrade",
                       help="Swagger 2.0 -> OpenAPI 3.x (FastMCP needs 3.x)")
    u.add_argument("source", help="Swagger 2.0 file (.yaml/.json)")
    u.add_argument("-o", "--output", help="output file (default: stdout)")
    u.add_argument("--format", choices=["yaml", "json"])
    u.add_argument("--openapi-version", choices=["3.0", "3.1"], default="3.0")
    u.add_argument("--strict", action="store_true",
                   help="fail when the conversion would need any assumption "
                        "or lossy transformation")
    u.set_defaults(fn=cmd_upgrade)

    v = sub.add_parser("validate",
                       help="check a spec (or WSDL) for FastMCP convertibility")
    v.add_argument("source", help="OpenAPI spec (.yaml/.json) or WSDL path/URL")
    v.add_argument("--format", choices=["text", "json"], default="text",
                   help="output format: human text or the verify() report "
                        "as JSON")
    v.set_defaults(fn=cmd_validate)

    s = sub.add_parser("serve",
                       help="reference MCP runtime (requires [mcp] extra)")
    s.add_argument("source", help="OpenAPI spec (.yaml/.json) or WSDL path/URL")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--path", default="/mcp")
    s.add_argument("--validate-output", action="store_true",
                   help="validate tool output against the response schema")
    _add_bridge_args(s)
    s.set_defaults(fn=cmd_serve)

    g = sub.add_parser("agentize",
                       help="LLM으로 빈 설명을 채워 agent가 쓸 수 있는 "
                            "tool 표면을 만든다 (requires an [llm-*] extra)")
    g.add_argument("source", help="OpenAPI spec (.yaml/.json) or WSDL path/URL")
    g.add_argument("-o", "--output", help="output file (default: stdout)")
    g.add_argument("--format", choices=["yaml", "json"])
    g.add_argument("--dry-run", action="store_true",
                   help="적용 없이 보강 대상만 출력한다 (LLM 호출 없음)")
    g.add_argument("--provider", choices=["anthropic", "openai"],
                   help="env SPEC2OPENAPI_LLM_PROVIDER")
    g.add_argument("--model", help="모델 ID (provider별 기본값 사용)")
    g.add_argument("--target", default="properties,desc,params,examples,enums",
                   help="보강 대상 (쉼표 구분). 나열 순서가 우선순위")
    g.add_argument("--rename-tools", action="store_true",
                   help="operationId를 읽기 좋은 이름으로 교체한다 "
                        "(기존 MCP 클라이언트의 tool 이름이 깨질 수 있다)")
    g.add_argument("--allow-speculative", action="store_true",
                   help="근거 없는 제안도 적용한다")
    g.add_argument("--keep-low-value", action="store_true",
                   help="저품질 기존 설명을 보존한다 (빈 곳만 채움)")
    g.add_argument("--overwrite", action="store_true",
                   help="기존 설명을 품질과 무관하게 전부 교체한다")
    g.add_argument("--self-check", action="store_true",
                   help="최종 tool payload를 다시 점검해 모호한 op을 보고한다")
    g.add_argument("--language", default="the language already used in the "
                                         "spec, or English if none")
    g.add_argument("--max-ops", type=int, default=None,
                   help="operation 수 상한. 초과 시 호출 전에 중단한다")
    g.set_defaults(fn=cmd_agentize)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError, OSError) as exc:
        # expected user-input errors: one-line message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
