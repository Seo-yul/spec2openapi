"""Opt-in sweep over real-world Swagger 2.0 definitions from APIs.guru.

Deselected by default; run with:  python -m pytest -m corpus

Per spec, three oracles: convert(3.0) + openapi-spec-validator,
convert(3.1) + validator, and a FastMCP round-trip on the 3.0 output.
Specs listed in known_failures.txt may fail without breaking the sweep;
anything else failing is a regression. Downloads are cached under
~/.cache/spec2openapi-corpus (override: SPEC2OPENAPI_CORPUS_CACHE).
Sample size: SPEC2OPENAPI_CORPUS_SAMPLE (default 200), stratified to at
most 2 specs per provider so no single vendor dominates.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

pytestmark = pytest.mark.corpus

LIST_URL = "https://api.apis.guru/v2/list.json"
CACHE = Path(os.environ.get(
    "SPEC2OPENAPI_CORPUS_CACHE",
    Path.home() / ".cache" / "spec2openapi-corpus",
))
SAMPLE = int(os.environ.get("SPEC2OPENAPI_CORPUS_SAMPLE", "200"))
SIZE_CAP = 1_500_000
_UA = {"User-Agent": "spec2openapi-corpus-tests"}


def _fetch(url: str, cache_name: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / cache_name
    if f.exists():
        return f.read_bytes()
    # some directory URLs contain spaces etc. — quote the path portion
    url = quote(url, safe=":/?&=%#@!$'()*+,;[]")
    with urlopen(Request(url, headers=_UA), timeout=30) as resp:
        data = resp.read()
    f.write_bytes(data)
    return data


def _stratified_sample() -> list[tuple[str, str]]:
    """Swagger 2.0 specs, at most 2 per provider, up to SAMPLE."""
    data = json.loads(_fetch(LIST_URL, "list.json"))
    by_provider: dict[str, list[tuple[str, str]]] = {}
    for name, api in sorted(data.items()):
        v = api["versions"][api["preferred"]]
        if not str(v.get("openapiVer", "")).startswith("2"):
            continue
        if not v.get("swaggerUrl"):
            continue
        by_provider.setdefault(name.split(":")[0], []).append(
            (name, v["swaggerUrl"])
        )
    picked: list[tuple[str, str]] = []
    for round_i in range(2):  # 1st spec of every provider, then a 2nd
        for provider in sorted(by_provider):
            specs = by_provider[provider]
            if len(specs) > round_i and len(picked) < SAMPLE:
                picked.append(specs[round_i])
    return picked[:SAMPLE]


def _allowlist() -> set[str]:
    text = (Path(__file__).parent / "known_failures.txt").read_text()
    entries = set()
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line:
            entries.add(line)
    return entries


def test_corpus_sweep():
    pytest.importorskip("fastmcp")
    pytest.importorskip("openapi_spec_validator")
    import anyio
    import httpx
    from fastmcp import Client, FastMCP
    from openapi_spec_validator import validate as osv_validate

    from spec2openapi import convert_swagger

    try:
        picked = _stratified_sample()
    except OSError as exc:
        pytest.skip(f"corpus source unreachable: {exc}")

    allow = _allowlist()
    failures: list[tuple[str, list[str]]] = []
    allowed_failures = tested = skipped = 0

    logging.disable(logging.CRITICAL)
    try:
        for name, url in picked:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            try:
                raw = _fetch(url, f"{safe}.json")
            except Exception:  # any fetch problem: skip, don't abort sweep
                skipped += 1
                continue
            if len(raw) > SIZE_CAP:
                skipped += 1
                continue
            try:
                src = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            # the directory sometimes serves an OpenAPI 3 document even for
            # entries whose metadata says 2.x — content is the truth
            if not str(src.get("swagger", "")).startswith("2"):
                skipped += 1
                continue
            tested += 1

            errs: list[str] = []
            for ver in ("3.0", "3.1"):
                out = None
                try:
                    out = convert_swagger(src, openapi_version=ver)
                    osv_validate(out)
                except Exception as exc:  # collect, don't abort the sweep
                    errs.append(
                        f"{ver}: {type(exc).__name__}: {str(exc)[:140]}"
                    )
                if ver == "3.0" and out is not None:
                    try:
                        client = httpx.AsyncClient(
                            base_url="http://corpus.invalid"
                        )
                        mcp = FastMCP.from_openapi(
                            openapi_spec=out, name="corpus", client=client
                        )

                        async def _tools():
                            async with Client(mcp) as c:
                                return await c.list_tools()

                        anyio.run(_tools)
                    except Exception as exc:
                        errs.append(
                            f"fastmcp: {type(exc).__name__}: {str(exc)[:140]}"
                        )
            if errs:
                if name in allow:
                    allowed_failures += 1
                else:
                    failures.append((name, errs))
    finally:
        logging.disable(logging.NOTSET)

    report = "\n".join(f"- {n}: " + " | ".join(e) for n, e in failures)
    assert not failures, (
        f"{len(failures)} corpus regression(s) "
        f"(tested={tested}, skipped={skipped}, allowed={allowed_failures}):\n"
        f"{report}"
    )
