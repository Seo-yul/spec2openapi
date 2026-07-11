# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **WSDL → OpenAPI 3.0/3.1 conversion** — document/literal and rpc/literal
  bindings, SOAP 1.1/1.2 (dual-port dedup, `--prefer-soap12`), nested complex
  types, arrays, attributes, `nillable`, inheritance (flattened
  `complexContent` extensions), `simpleContent` (value + attributes),
  `choice` (optional members + `x-soap-choice`), default values, recursive
  types, multi-service/multi-port WSDLs, operationId collision handling.
- **XSD facets and documentation carried into schemas** — enumerations,
  `pattern`, length and numeric bounds, `fractionDigits` (→ `multipleOf`),
  and `xsd:annotation`/`wsdl:documentation` → `description`, including from
  `xsd:import`-ed schemas.
- **`x-soap` vendor extensions** — SOAPAction, SOAP version, endpoint,
  wrapper element QNames, `soap:header` parts and declared faults, plus
  OpenAPI `xml` annotations carrying everything a call layer needs to
  serialize JSON ↔ literal XML.
- **Swagger 2.0 → OpenAPI 3.x upgrader** (`spec2openapi upgrade`,
  `convert_swagger()`) — full mechanical mapping (servers, requestBody,
  formData/multipart, `collectionFormat` → `style`/`explode`, `$ref`
  rewriting, security schemes, `type: file`, `x-nullable`, discriminator).
  Deterministic defaults for missing information recorded in
  `x-s2o.assumptions`; untranslatable constructs preserved as `x-`
  extensions and listed in `x-s2o.lossy`.
- **FastMCP compatibility guarantee** — operationIds generated in FastMCP's
  tool-name alphabet (`[A-Za-z0-9_]`, unique, ≤64 chars) so that
  *tool name == operationId*; `spec2openapi validate` verifies it with
  static checks, `openapi-spec-validator`, and a real
  `FastMCP.from_openapi()` round-trip.
- **CLI** — `convert` / `inspect` / `upgrade` / `validate` / `serve`
  (Swagger 2.0 input auto-detected and upgraded in memory).
- **Reference MCP runtime** (optional `[mcp]` extra) — SOAP bridge (custom
  httpx transport) converting MCP tool calls JSON ↔ SOAP envelopes, SOAP
  fault → MCP tool error mapping, Basic auth and WS-Security UsernameToken,
  fixed Dockerfile and Kubernetes ConfigMap/Deployment examples.

### Security
- All XML parsing (WSDL/XSD scraping, SOAP response parsing) disables DTD
  loading, entity resolution, and parser-level network access.
- `--forbid-external` (CLI) / `forbid_external=True` (API) refuses fetching
  remote `wsdl:`/`xsd:` imports — SSRF mitigation for WSDLs from untrusted
  sources; local relative imports keep working.
- libxml2 depth/size limits now apply by default; `--huge-tree` opts out
  for very large trusted WSDLs.
- CI workflow token restricted to read-only; the reference Docker image
  runs as a non-root user.
