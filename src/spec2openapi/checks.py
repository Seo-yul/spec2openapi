"""Structured verification of converted specs.

`verify(spec)` runs a registry of checks over an OpenAPI document and
returns a deterministic, JSON-serializable report. Adapted from the
design of modelcontextprotocol/conformance: structured per-check results,
normative citations (refs), and explicit skip-vs-fail separation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckRef:
    """A normative citation backing a check (spec section, contract doc)."""
    id: str
    url: str = ""


@dataclass(frozen=True)
class CheckResult:
    id: str
    status: str                # "pass" | "warn" | "fail" | "skip"
    message: str = ""
    location: str = ""
    refs: tuple[CheckRef, ...] = ()
    data: dict | None = None


@dataclass(frozen=True)
class VerifyReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.status != "fail" for r in self.results)

    @property
    def complete(self) -> bool:
        return all(r.status != "skip" for r in self.results)

    def to_dict(self) -> dict:
        summary = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
        for r in self.results:
            summary[r.status] += 1
        return {
            "ok": self.ok,
            "complete": self.complete,
            "summary": summary,
            "results": [
                {
                    "id": r.id, "status": r.status, "message": r.message,
                    "location": r.location,
                    "refs": [{"id": ref.id, "url": ref.url} for ref in r.refs],
                    "data": r.data,
                }
                for r in self.results
            ],
        }
