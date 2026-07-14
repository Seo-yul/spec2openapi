"""spec2openapi: SOAP/WSDL -> FastMCP-ready OpenAPI specs.

Core API (no MCP dependencies): convert_wsdl, load_spec, dump_spec,
spec_has_soap, parse_wsdl, build_spec.

Optional MCP runtime (pip install 'spec2openapi[mcp]'): from_openapi_spec,
from_wsdl, BridgeOptions, SoapBridgeTransport.
"""

__version__ = "0.2.1"

from .convert import convert_wsdl, load_spec, spec_has_soap  # noqa: E402,F401
from .errors import ConversionError  # noqa: E402,F401
from .openapi import build_spec, dump_spec, to_openapi_31  # noqa: E402,F401
from .parser import parse_wsdl  # noqa: E402,F401
from .swagger import convert_swagger, is_swagger2  # noqa: E402,F401

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
    "ConversionError",
    # lazily loaded, require the [mcp] extra:
    "from_openapi_spec",
    "from_wsdl",
    "BridgeOptions",
    "SoapBridgeTransport",
]


def __getattr__(name: str):
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
