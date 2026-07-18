"""A Swagger-only consumer must not pay for the SOAP stack (#86).

Run in a subprocess so this test's own imports cannot pollute the
measurement.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def _run(code: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_plain_import_leaves_soap_stack_out():
    out = _run("""
        import sys
        import spec2openapi
        spec2openapi.convert_swagger(
            {"swagger": "2.0", "info": {"title": "t", "version": "1"},
             "paths": {}})
        print([m for m in ("zeep", "lxml", "httpx") if m in sys.modules])
    """)
    assert out == "[]"


def test_parse_wsdl_still_reachable_from_package_root():
    out = _run("""
        import sys
        from spec2openapi import parse_wsdl
        print(callable(parse_wsdl), "zeep" in sys.modules)
    """)
    assert out == "True True"
