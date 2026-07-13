# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Swagger upgrader: `in: path` parameters are now forced to
  `required: true` (mandatory in OpenAPI 3); a source that omitted it no
  longer yields an invalid spec, and the coercion is recorded in
  `x-s2o.assumptions` (#15).
- Swagger upgrader no longer emits invalid `securitySchemes` (#17): an
  unknown security `type`, an `apiKey` missing `name`/`in`, or an `oauth2`
  flow missing its required URL(s) is dropped and recorded in
  `x-s2o.lossy` instead of producing a spec that fails validation.
- Swagger upgrader completes a partial `info` object (missing `title` or
  `version`, both required in OpenAPI 3) and injects a required path
  parameter for any `{template}` segment that lacks one, so the output no
  longer fails validation; both are recorded in `x-s2o.assumptions` (#19).
- Swagger upgrader respects parameter location (#21): `allowEmptyValue` is
  kept only on query parameters; `collectionFormat` maps to a location-legal
  `style` (path/header use `simple` instead of the invalid `form`); a
  `formData` parameter without a name is dropped (recorded in
  `x-s2o.lossy`) instead of crashing.
- Swagger upgrader hardened against non-conformant input (#23): a
  non-boolean `required` is coerced to a boolean; a non-path parameter
  without a `name` is dropped; a JSON-Schema `type` array is collapsed to
  a single type + `nullable` for 3.0 (re-expanded for 3.1); a
  `discriminator` object without `propertyName` is dropped. All recorded
  in `x-s2o`.

## [0.2.0] - 2026-07-13

### Changed
- Documentation repositioned to make the SOAP vs Swagger distinction
  explicit: Swagger 2.0 converts to a standard REST OpenAPI document that
  any runtime serves unchanged, while SOAP/WSDL converts to OpenAPI +
  `x-soap` and **requires the `[mcp]` bridge to be served** (#6).

### Fixed
- `spec2openapi serve` without the `[mcp]` extra now prints the install
  hint and exits with code 2 instead of crashing with a raw
  `ModuleNotFoundError` traceback (#4).
- **Core converter correctness** (#8):
  - operationIds are re-checked for uniqueness *after* normalization and
    64-char truncation, so two operations can no longer collide onto the
    same path and silently drop one (WSDL and Swagger paths).
  - a top-level `<xsd:choice>` is now detected (previously only nested
    choices produced `x-soap-choice`); choices whose branches are
    `<sequence>`s no longer mark every branch field `required`.
  - a WSDL type named `SoapFault` is no longer clobbered by the built-in
    fault schema.
  - attribute `xml.name` uses the real attribute name, not zeep's mangled
    `attr__id` key when an element and attribute share a name.
  - Swagger upgrader: operationId normalizing to empty falls back to a
    generated id; operation-level parameters override path-level ones
    instead of duplicating; `basePath` without a leading slash is
    normalized; boolean `exclusiveMinimum/Maximum` no longer leak into
    3.1 output; `example`/`default`/`enum` data values are no longer
    rewritten; paths-level vendor extensions and `$ref` path items are
    preserved; more silently-dropped constructs (second body param,
    formData `collectionFormat`, root `x-` extensions, missing `info`,
    oauth2 without `flow`) are now recorded in `x-s2o`.
- **Runtime (bridge/serve/CLI) robustness** (#10):
  - one-way SOAP operations with an empty 2xx body now succeed instead of
    failing with a spurious `InvalidXML` fault.
  - HTTP errors without a SOAP fault body report the real status code and
    body excerpt instead of a misleading `InvalidXML`/`NoBody` fault.
  - `x-soap-choice` is enforced when serializing: setting more than one
    member (or none of a required group) returns a client-side error
    instead of emitting invalid XML.
  - `SPEC2OPENAPI_TIMEOUT`/`VERIFY`/`TRUST_ENV` parsing no longer crashes
    on bad values (warns and falls back); boolean env vars are
    case-insensitive.
  - non-canonical XML booleans (e.g. `TRUE`) are no longer silently read
    as `False`.
  - CLI user-input errors (missing file, wrong type, malformed WSDL, bad
    output path) print a one-line `error:` and exit 2 instead of dumping a
    traceback.
  - WSDL files with a UTF-8 BOM and extension-less WSDL URLs are routed
    correctly; output `--format` inference is case-insensitive.
  - `validate` no longer flags path-level vendor extensions as operations,
    and validates REST specs that omit a `servers` entry.
  - `serve` warns when a spec mixes SOAP and REST operations (the
    reference runtime routes everything through the SOAP bridge).

## [0.1.0] - 2026-07-11

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

[Unreleased]: https://github.com/Seo-yul/spec2openapi/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Seo-yul/spec2openapi/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Seo-yul/spec2openapi/releases/tag/v0.1.0
