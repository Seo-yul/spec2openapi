"""spec2openapi: legacy API specs (SOAP/WSDL, Swagger 2.0) ->
FastMCP-ready OpenAPI 3.x documents.

Core API (the zeep/lxml SOAP stack loads only on first SOAP use):

    convert_swagger     Swagger 2.0 dict -> OpenAPI 3.0/3.1 dict
    convert_wsdl        WSDL path/URL -> OpenAPI dict with x-soap
    load_spec           read a spec from a path or http(s) URL
    dump_spec           serialize a spec to YAML/JSON text
    check_fastmcp_ready static FastMCP-readiness problems ([] == ready)
    is_swagger2 / spec_has_soap / to_openapi_31 / parse_wsdl / build_spec

Everything the converters assume or cannot translate is recorded in the
output's `x-s2o` block; failures raise ConversionError (a ValueError).
The public API is exactly `__all__`.

Optional MCP runtime (pip install 'spec2openapi[mcp]'): from_openapi_spec,
from_wsdl, BridgeOptions, SoapBridgeTransport.
"""

__version__ = "0.3.0"

from typing import TYPE_CHECKING

from .convert import convert_wsdl, load_spec, spec_has_soap  # noqa: E402,F401
from .errors import ConversionError  # noqa: E402,F401
from .openapi import (  # noqa: E402,F401
    build_spec,
    check_fastmcp_ready,
    dump_spec,
    to_openapi_31,
)
from .swagger import convert_swagger, is_swagger2  # noqa: E402,F401

if TYPE_CHECKING:  # loaded lazily at runtime (pulls the zeep/lxml stack)
    from .parser import parse_wsdl  # noqa: F401

_MCP_ATTRS = {
    "from_openapi_spec": "server",
    "from_wsdl": "server",
    "BridgeOptions": "bridge",
    "SoapBridgeTransport": "bridge",
}

__all__ = [
    "__version__",
    "convert_wsdl",
    "load_spec",
    "dump_spec",
    "spec_has_soap",
    "parse_wsdl",
    "build_spec",
    "to_openapi_31",
    "convert_swagger",
    "is_swagger2",
    "check_fastmcp_ready",
    "ConversionError",
    # lazily loaded, require the [mcp] extra:
    "from_openapi_spec",
    "from_wsdl",
    "BridgeOptions",
    "SoapBridgeTransport",
]


def __getattr__(name: str):
    if name == "parse_wsdl":
        # SOAP-only entry point: importing it pulls zeep/lxml, which a
        # Swagger-only consumer should not pay for at import time
        from .parser import parse_wsdl

        return parse_wsdl
    if name in _MCP_ATTRS:
        import importlib

        try:
            module = importlib.import_module(f".{_MCP_ATTRS[name]}", __name__)
        except ImportError as exc:
            raise ImportError(
                f"spec2openapi.{name} requires optional dependencies; "
                "install them with: pip install 'spec2openapi[mcp]'"
            ) from exc
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
