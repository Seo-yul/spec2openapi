"""Shared exception types."""
from __future__ import annotations


class ConversionError(ValueError):
    """Raised when a source document cannot be faithfully converted to a
    valid OpenAPI 3 document (required data missing or unrepresentable).

    Subclasses ValueError so the CLI surfaces it as a clean one-line error
    (exit code 2) rather than a traceback.
    """
