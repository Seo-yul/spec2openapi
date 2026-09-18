"""Core conversion API (no MCP/httpx2 dependencies).

    spec = spec2openapi.convert_wsdl("https://host/service?wsdl")
    spec2openapi.dump_spec(spec)          # yaml/json text
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import ConversionError
from .openapi import (  # noqa: F401  (re-exported)
    _normalize_openapi_version,
    _operations,
    build_spec,
    dump_spec,
)


def convert_wsdl(
    source: str | os.PathLike | None = None,
    *,
    content: str | bytes | None = None,
    files: dict[str, str | bytes] | None = None,
    entry: str | None = None,
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
    """WSDL -> OpenAPI dict with x-soap extensions.

    The input is exactly one of `source` (path, http(s) URL, or zip
    bundle), `content` (the document itself as str/bytes; zip bytes are
    detected), or `files` (an in-memory bundle whose relative imports
    resolve within it, with `entry` naming the document to convert).

    Set forbid_external=True when the WSDL comes from an untrusted source
    (refuses to fetch remote wsdl:/xsd: imports).
    """
    if source is not None and not isinstance(source, (str, os.PathLike)):
        raise ConversionError(
            "convert_wsdl expects a WSDL file path or URL (str), "
            f"got {type(source).__name__}"
        )
    # reject an unsupported target version before the (possibly remote)
    # WSDL parse, not after
    openapi_version = _normalize_openapi_version(openapi_version)
    from .parser import parse_wsdl  # defers zeep/lxml to first SOAP use

    parsed = parse_wsdl(
        source, content=content, files=files, entry=entry,
        service=service, port=port,
        prefer_soap12=prefer_soap12, strict=strict,
        forbid_external=forbid_external, huge_tree=huge_tree,
    )
    return build_spec(
        parsed, title=title, version=version,
        base_path=base_path, openapi_version=openapi_version,
    )


def _fetch_url(src: str) -> bytes:
    """GET an http(s) URL; failures are ConversionError."""
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    from . import __version__

    req = Request(src, headers={"User-Agent": f"spec2openapi/{__version__}"})
    try:
        with urlopen(req, timeout=30) as resp:  # noqa: S310 (user-supplied URL)
            return resp.read()
    except (URLError, OSError) as exc:
        raise ConversionError(f"{src}: could not fetch — {exc}") from exc


def load_spec(path: str | Path) -> dict[str, Any]:
    """Load an OpenAPI/Swagger spec from a .yaml/.yml/.json file or an
    http(s) URL (parity with ``convert``, which accepts WSDL URLs)."""
    src = str(path)
    if src.startswith(("http://", "https://")):
        return _parse_spec(_fetch_url(src), src,
                           is_json=src.lower().endswith(".json"))
    p = Path(path)
    return _parse_spec(p.read_bytes(), str(p),
                       is_json=p.suffix.lower() == ".json")


def _parse_spec(data: bytes, label: str, *, is_json: bool) -> dict[str, Any]:
    """Decode and parse a JSON/YAML spec document."""
    import yaml

    # decode loudly: replacing invalid bytes would silently corrupt text
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConversionError(f"{label}: not valid UTF-8 — {exc}") from exc
    # parse errors are prefixed with the source so the location is
    # traceable (json/yaml already report the line and column)
    try:
        if is_json:
            spec = json.loads(text)
        elif text.lstrip().startswith("{"):
            try:
                spec = json.loads(text)
            except json.JSONDecodeError:
                # flow-style YAML ({key: value}) is not JSON; YAML is a
                # superset of JSON, so it reads both
                spec = yaml.safe_load(text)
        else:
            spec = yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"{label}: invalid JSON — {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConversionError(f"{label}: invalid YAML — {exc}") from exc
    if not isinstance(spec, dict):
        raise ConversionError(
            f"{label}: not a valid OpenAPI/Swagger document "
            f"(parsed as {type(spec).__name__}, expected a mapping)"
        )
    return spec


def spec_has_soap(spec: dict[str, Any]) -> bool:
    """True if any operation carries the x-soap extension (i.e. calling
    it requires the SOAP bridge). A malformed document is simply False.

    Built on openapi._operations, the single source of truth for "what is
    an operation" (#142): an earlier hand-rolled version of this loop
    walked every value under a path item with no method-name filter at
    all, so a non-operation dict entry with a coincidentally truthy
    'x-soap' key could produce a false positive."""
    return any(op.get("x-soap") for _, _, op in _operations(spec))
