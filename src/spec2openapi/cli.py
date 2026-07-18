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

_MCP_HINT = "install the MCP runtime extras first: pip install 'spec2openapi[mcp]'"


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

    spec = convert_wsdl(
        args.wsdl, title=args.title, version=args.spec_version,
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
    """Static checks + optional FastMCP round-trip on a spec (or WSDL)."""
    from .openapi import _FASTMCP_NORM_RE, _operations, check_fastmcp_ready

    spec = _load_or_convert(args.source)
    problems = check_fastmcp_ready(spec)
    op_ids = [op.get("operationId")
              for _, _, op in _operations(spec) if op.get("operationId")]

    print(f"operations        : {len(op_ids)}")
    print(f"component schemas : {len(spec.get('components', {}).get('schemas', {}))}")

    try:  # optional deep validation
        from openapi_spec_validator import validate as osv_validate

        osv_validate(spec)
        print("openapi-spec-validator: OK")
    except ImportError:
        print("openapi-spec-validator: not installed (skipped)")
    except Exception as exc:
        problems.append(f"openapi-spec-validator: {exc}")

    try:  # FastMCP round-trip: the compatibility this project guarantees
        import anyio
        import httpx
        from fastmcp import Client, FastMCP

        # supply a dummy client so specs without a `servers` entry still
        # convert — validate measures tool convertibility, not deployment
        dummy = httpx.AsyncClient(base_url="http://spec2openapi.invalid")
        mcp = FastMCP.from_openapi(
            openapi_spec=spec, name="validate", client=dummy
        )

        async def _tools():
            async with Client(mcp) as client:
                return await client.list_tools()

        tools = anyio.run(_tools)
        print(f"FastMCP round-trip: OK ({len(tools)} tools)")
        for tool in sorted(tools, key=lambda t: t.name):
            schema = getattr(tool, "inputSchema", None) or {}
            params = ", ".join((schema.get("properties") or {}).keys())
            print(f"  - {tool.name}({params})")
        tool_names = {t.name for t in tools}

        def _norm(s: str) -> str:  # FastMCP tool-name normalization
            return _FASTMCP_NORM_RE.sub("_", s)

        renamed = {o for o in op_ids
                   if o not in tool_names and _norm(o) in tool_names}
        for o in sorted(renamed):
            print(f"note: operationId '{o}' is exposed as tool "
                  f"'{_norm(o)}' (FastMCP normalization)")
        missing = {o for o in op_ids
                   if o not in tool_names and _norm(o) not in tool_names}
        if missing:
            problems.append(f"operations not materialized as tools: {sorted(missing)}")
    except ImportError:
        print(f"FastMCP round-trip: fastmcp not installed (skipped) - {_MCP_HINT}")
    except Exception as exc:
        problems.append(f"FastMCP round-trip failed: {exc}")

    if problems:
        print("\nFAIL")
        for p in problems:
            print(f"  ! {p}")
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
    c.add_argument("wsdl", help="WSDL path or URL")
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

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError, OSError) as exc:
        # expected user-input errors: one-line message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
