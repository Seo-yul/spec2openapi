# spec2openapi

> Convert legacy API specifications — SOAP/WSDL and Swagger 2.0 — into **FastMCP-ready OpenAPI 3.x** documents.

[![CI](https://github.com/Seo-yul/spec2openapi/actions/workflows/ci.yml/badge.svg)](https://github.com/Seo-yul/spec2openapi/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/spec2openapi)](https://pypi.org/project/spec2openapi/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Code of Conduct](https://img.shields.io/badge/code%20of%20conduct-Contributor%20Covenant-purple.svg)](CODE_OF_CONDUCT.md)

[한국어 문서 (Korean README)](README.ko.md)

---

MCP (Model Context Protocol) tooling such as [FastMCP](https://gofastmcp.com) can turn an OpenAPI 3.x document into an MCP server automatically — but enterprises are full of services described only by WSDL or Swagger 2.0. `spec2openapi` closes that gap:

```
WSDL ─────────┐
              ├──(spec2openapi)──> OpenAPI 3.x (+ x-soap extensions) ──> FastMCP.from_openapi() ──> MCP tools
Swagger 2.0 ──┘
```

The two inputs produce two kinds of output — this distinction matters:

- **Swagger 2.0 → a plain, standard OpenAPI 3.x document.** The paths are the real REST endpoints. Any OpenAPI-driven runtime (FastMCP, or your own httpx-based server) serves it with **zero runtime changes** — just point it at the converted spec.
- **WSDL → OpenAPI 3.x + an `x-soap` contract.** The generated `/operations/...` paths are **not real REST endpoints**; each tool call must be serialized to a SOAP envelope, sent to the SOAP endpoint, and the XML response parsed back to JSON. That logic is **not** part of a standard OpenAPI runtime — it lives in the **SOAP bridge shipped in the `[mcp]` extra**. Serving a SOAP-converted spec with a plain OpenAPI/httpx runtime will POST JSON to the SOAP endpoint and fail every call.

So `spec2openapi` is a **converter for Swagger 2.0**, and a **converter + runtime contract (with a reference bridge) for SOAP**. See [How SOAP calls work](#how-soap-calls-work-the-x-soap-contract) below.

The fixed-runtime deployment model — build one image, swap the spec via a Kubernetes ConfigMap to mass-produce MCP servers — applies to both, as long as the image includes the `[mcp]` extra when serving SOAP specs.

## Features

- **WSDL → OpenAPI 3.0/3.1** — document/literal and rpc/literal bindings, SOAP 1.1/1.2, nested complex types, arrays, attributes, `nillable`, inheritance (flattened `complexContent` extensions), `simpleContent` (text value + attributes), `choice` (members become optional + `x-soap-choice`), default values, recursive types, multi-service/multi-port WSDLs with automatic dedup.
- **XSD facets & docs carried into tool schemas** — enumerations, `pattern`, length and numeric bounds, `fractionDigits` (→ `multipleOf`), and `xsd:annotation` documentation are extracted (including from `xsd:import`-ed schemas) so LLMs see well-described, well-constrained tool arguments.
- **`x-soap` contract** — SOAPAction, SOAP version, endpoint, wrapper element QNames, `soap:header` parts and declared faults are embedded as vendor extensions; OpenAPI `xml` annotations carry everything a call layer needs to serialize JSON ↔ literal XML.
- **Swagger 2.0 → OpenAPI 3.x upgrade** — full mechanical mapping (servers, requestBody, formData/multipart, parameter schema wrapping, `collectionFormat` → `style`/`explode`, `$ref` rewriting, security schemes, `type: file`, `x-nullable`, discriminator). Every assumption made for missing information is recorded in `x-s2o.assumptions`; untranslatable constructs are preserved as `x-` extensions and listed in `x-s2o.lossy`.
- **FastMCP compatibility, guaranteed and verifiable** — operationIds are generated in FastMCP's tool-name alphabet (`[A-Za-z0-9_]`, unique, ≤64 chars) so *tool name == operationId*. `spec2openapi validate` proves it: static checks, `openapi-spec-validator`, and a real `FastMCP.from_openapi()` round-trip listing the resulting tools.
- **SOAP bridge — required to *serve* SOAP specs** — `pip install "spec2openapi[mcp]"` adds the bridge (custom httpx transport) that implements the `x-soap` contract, plus FastMCP glue, a fixed Dockerfile, and Kubernetes examples. SOAP faults map to MCP tool errors. **Swagger-converted (pure REST) specs do not need this** — any OpenAPI runtime serves them. Only SOAP-converted specs require the bridge at runtime.

## Installation

```bash
pip install spec2openapi          # converter + CLI (zeep, lxml, PyYAML)
pip install "spec2openapi[mcp]"   # + SOAP bridge & runtime — required to serve SOAP specs
```

> The core install is enough to **convert** any spec and to serve **Swagger-converted (REST)** specs from your own runtime. The `[mcp]` extra is required only to **serve SOAP-converted** specs (it provides the bridge that turns JSON tool calls into SOAP envelopes).

## Quick start

### CLI

```bash
# See what a WSDL contains (operations, headers, faults, style)
spec2openapi inspect https://legacy-host/OrderService?wsdl

# WSDL -> OpenAPI
spec2openapi convert https://legacy-host/OrderService?wsdl -o orders.openapi.yaml

# Swagger 2.0 -> OpenAPI 3.x (assumptions reported on stderr)
spec2openapi upgrade swagger2.json -o service.openapi.yaml

# Prove the spec converts cleanly into MCP tools
spec2openapi validate orders.openapi.yaml

# Reference MCP runtime (requires the [mcp] extra)
spec2openapi serve orders.openapi.yaml --transport http --port 8000
```

```text
$ spec2openapi validate orders.openapi.yaml
operations        : 2
component schemas : 3
openapi-spec-validator: OK
FastMCP round-trip: OK (2 tools)
  - CreateOrder(customer, items, note)
  - GetOrder(orderId)

OK: spec is FastMCP-convertible
```

### Library

```python
import spec2openapi
from spec2openapi import ConversionError

# Swagger 2.0 -> OpenAPI dict (input may be a path or an http(s) URL)
try:
    legacy = spec2openapi.load_spec("swagger2.json")
    spec = spec2openapi.convert_swagger(legacy, openapi_version="3.1")
except ConversionError as exc:      # every failure path raises this
    raise SystemExit(f"conversion failed: {exc}")

# everything the converter assumed or could not translate, per document
report = spec.get("x-s2o", {})
report.get("assumptions", [])       # e.g. "missing consumes -> application/json"
report.get("lossy", [])             # e.g. "collectionFormat 'tsv' preserved as x-"
# pipelines that must not accept guesses: convert_swagger(legacy, strict=True)

# the FastMCP-readiness contract as a function (empty list == ready)
problems = spec2openapi.check_fastmcp_ready(spec)

# WSDL -> OpenAPI dict (zeep loads on first SOAP use, not at import)
spec = spec2openapi.convert_wsdl(
    "https://legacy-host/OrderService?wsdl",
    forbid_external=True,           # refuse remote imports from untrusted WSDLs
)

print(spec2openapi.dump_spec(spec))            # YAML text (fmt="json" for JSON)

# Optional [mcp] extra: run it as an MCP server right away
mcp = spec2openapi.from_openapi_spec(spec)
mcp.run(transport="http", host="0.0.0.0", port=8000)
```

The public API is exactly `spec2openapi.__all__` (typed, PEP 561); anything
else is internal and may change without notice. All entry points report
failures as `ConversionError` (a `ValueError` subclass). Returned documents
may share substructures with the input mapping (`example`/`default`/`enum`
and `x-*` values are not deep-copied) — `copy.deepcopy` the result before
mutating it if you keep using the input.

## How SOAP calls work (the `x-soap` contract)

The generated paths (`/operations/...`) are *not* real REST endpoints — a SOAP translation layer must build the actual call. Everything it needs ships inside the spec:

| Field (`paths.*.post.x-soap`) | Meaning |
|---|---|
| `operation` / `service` / `port` | WSDL names |
| `soapAction`, `soapVersion`, `style` | `"1.1"`/`"1.2"`, `document`/`rpc` |
| `endpoint` | `soap:address` (override at runtime) |
| `input` / `output` | wrapper element QNames |
| `headers[]` | `soap:header` parts with schema refs |
| `faults[]` | declared faults with schema refs |

Serialization rules (schema `xml` annotations): `xml.name`/`xml.namespace` (absent namespace = unqualified), `xml.attribute: true`, `xml.x-text: true` (simpleContent text), arrays repeat the element, and **property order = XSD sequence order** (do not alphabetize the document). `x-soap-choice` lists mutually exclusive property groups.

The `[mcp]` extra contains a verified implementation of this contract (`src/spec2openapi/bridge.py`) — use it directly (via `spec2openapi serve`) or as the reference for your own runtime. **There is no way to serve a SOAP-converted spec without an implementation of this contract**; a standard OpenAPI runtime cannot do it.

> **Mixed SOAP + REST specs.** The reference runtime routes *all* traffic through the SOAP bridge if *any* path carries `x-soap`, so REST operations in a mixed spec are not served correctly today. Keep SOAP and REST specs separate until this is addressed ([tracking issue](https://github.com/Seo-yul/spec2openapi/issues)).

## Handling missing information (Swagger 2.0)

Upgrading is favorable: OpenAPI 3.x is a superset of Swagger 2.0, so almost nothing must be invented. Where documents are genuinely underspecified, a three-tier policy applies:

1. **Deterministic, documented defaults** — missing `consumes`/`produces` → `application/json`; missing `operationId` → `{method}_{path}`; missing `host` → relative server `/`; missing `schemes` → `https`. All recorded in `x-s2o.assumptions`.
2. **Preserve, never drop** — constructs with no OpenAPI 3 equivalent (e.g. `collectionFormat: tsv`) are kept as `x-` extensions and listed in `x-s2o.lossy`.
3. **Verify the outcome** — `spec2openapi validate` runs the actual FastMCP round-trip; assumptions never block tool generation because tools only need paths and schemas.

Pipelines that must not accept guessed conversions can pass `--strict` to `upgrade` (or `strict=True` to `convert_swagger`): the conversion then fails with the full list of assumption/lossy records instead of applying them.

## Kubernetes: one image, many MCP servers

```bash
docker build -t spec2openapi:0.2.2 .
spec2openapi convert <wsdl> -o openapi.yaml
kubectl create configmap my-mcp-spec --from-file=openapi.yaml
kubectl apply -f k8s/example.yaml    # Deployment mounts /config/openapi.yaml
```

Only the ConfigMap changes per service; credentials live in a Secret (`SPEC2OPENAPI_ENDPOINT`, `SPEC2OPENAPI_AUTH` = `basic`|`wsse`, `SPEC2OPENAPI_USERNAME`/`PASSWORD`, `SPEC2OPENAPI_TIMEOUT`, `SPEC2OPENAPI_VERIFY`, `SPEC2OPENAPI_TRUST_ENV`). The MCP endpoint is `http://<service>:8000/mcp` (streamable HTTP).

## Limitations

rpc/encoded (skipped and recorded in `x-soap.skippedOperations`), MTOM/attachments, WS-Policy/WS-Addressing, and substitution groups are not supported. WS-Security support in the reference runtime is UsernameToken (PasswordText).

## Security

All XML parsing disables DTD loading, entity resolution, and parser-level network access. When converting WSDLs from **untrusted sources**, add `--forbid-external` (CLI) or `forbid_external=True` (API) to refuse fetching remote `wsdl:`/`xsd:` imports (SSRF mitigation; local relative imports still work). See [SECURITY.md](SECURITY.md) for the full notes and how to report vulnerabilities.

## Development

```bash
git clone https://github.com/Seo-yul/spec2openapi.git
cd spec2openapi
pip install -e ".[dev]"
python -m pytest tests/
```

The suite (190 tests) covers conversion units, the Swagger upgrader, envelope (de)serialization, end-to-end MCP-tool-call → mock-SOAP-server round-trips (rpc, simpleContent, choice, recursive trees, unqualified forms), FastMCP round-trips for every fixture × OpenAPI 3.0/3.1, and stress patterns (circular `$ref`s, deep nesting, large enums, cross-namespace name collisions, duplicate operation names across services, odd path characters, deep `allOf` chains). Generated samples live in [`examples/`](examples/).

An opt-in **corpus sweep** additionally runs the Swagger upgrader over a stratified sample of real-world Swagger 2.0 definitions from the public [APIs.guru](https://apis.guru) directory (fetched at test time, cached locally, never committed), checking every output against openapi-spec-validator (3.0 and 3.1) **and** a live `FastMCP.from_openapi()` round-trip:

```bash
python -m pytest -m corpus     # network required; see tests/corpus/
```

Pre-existing upstream findings are tracked in `tests/corpus/known_failures.txt` with issue links, so the sweep only fails on regressions.

## Project layout

```
src/spec2openapi/
  parser.py    WSDL parsing (zeep) + raw XSD scraping (facets/docs)
  schema.py    XSD -> JSON Schema (xml annotations, choice, simpleContent)
  openapi.py   OpenAPI 3.0/3.1 assembly + x-soap extensions
  swagger.py   Swagger 2.0 -> OpenAPI 3.x upgrader (x-s2o report)
  convert.py   core public API
  cli.py       convert / upgrade / inspect / validate / serve
  bridge.py    [mcp] SOAP bridge (httpx transport)
  server.py    [mcp] FastMCP glue
```

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md); by participating you agree to uphold it. Security issues should be reported privately per [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE) © Seoyul Yoon
