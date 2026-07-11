"""Core conversion API (no MCP/httpx dependencies).

    spec = spec2openapi.convert_wsdl("https://host/service?wsdl")
    spec2openapi.dump_spec(spec)          # yaml/json text
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .openapi import build_spec, dump_spec  # noqa: F401  (re-exported)
from .parser import parse_wsdl


def convert_wsdl(
    source: str,
    *,
    title: str | None = None,
    version: str = "1.0.0",
    base_path: str = "/operations",
    service: str | None = None,
    port: str | None = None,
    prefer_soap12: bool = False,
    strict: bool = False,
    openapi_version: str = "3.0",
    forbid_external: bool = False,
    huge_tree: bool = False,
) -> dict[str, Any]:
    """WSDL (path/URL) -> OpenAPI dict with x-soap extensions.

    Set forbid_external=True when the WSDL comes from an untrusted source
    (refuses to fetch remote wsdl:/xsd: imports).
    """
    parsed = parse_wsdl(
        source, service=service, port=port,
        prefer_soap12=prefer_soap12, strict=strict,
        forbid_external=forbid_external, huge_tree=huge_tree,
    )
    return build_spec(
        parsed, title=title, version=version,
        base_path=base_path, openapi_version=openapi_version,
    )


def load_spec(path: str | Path) -> dict[str, Any]:
    """Load an OpenAPI spec from a .yaml/.yml/.json file."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def spec_has_soap(spec: dict[str, Any]) -> bool:
    for item in spec.get("paths", {}).values():
        for method in (item or {}).values():
            if isinstance(method, dict) and method.get("x-soap"):
                return True
    return False
