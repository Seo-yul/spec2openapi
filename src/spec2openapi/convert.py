"""Core conversion API (no MCP/httpx dependencies).

    spec = spec2openapi.convert_wsdl("https://host/service?wsdl")
    spec2openapi.dump_spec(spec)          # yaml/json text
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ConversionError
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
    """Load an OpenAPI/Swagger spec from a .yaml/.yml/.json file."""
    import yaml

    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    # parse errors are prefixed with the file path so the location is
    # traceable (json/yaml already report the line and column)
    try:
        if p.suffix.lower() == ".json" or text.lstrip().startswith("{"):
            spec = json.loads(text)
        else:
            spec = yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"{p}: invalid JSON — {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConversionError(f"{p}: invalid YAML — {exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError(
            f"{p}: not a valid OpenAPI/Swagger document "
            f"(parsed as {type(spec).__name__}, expected a mapping)"
        )
    return spec


def spec_has_soap(spec: dict[str, Any]) -> bool:
    for item in spec.get("paths", {}).values():
        for method in (item or {}).values():
            if isinstance(method, dict) and method.get("x-soap"):
                return True
    return False
