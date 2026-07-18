# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The package now ships a `py.typed` marker (PEP 561), so mypy/pyright
  pick up the library's type hints in consumer projects (#85).
- Opt-in real-world corpus test suite (`python -m pytest -m corpus`): a
  stratified sample of public Swagger 2.0 definitions from APIs.guru is
  converted and checked against the 3.0/3.1 validators and a live FastMCP
  round-trip, with a tracked known-failures allowlist so only regressions
  fail (#71).
- `upgrade`/`validate`/`serve` (and `load_spec`) accept http(s) URLs for
  Swagger/OpenAPI sources, matching `convert`'s WSDL-URL support; fetch
  errors are reported with the URL (#69).
- `--strict` on `spec2openapi upgrade` (and `strict=True` on
  `convert_swagger`): fail with the full list of assumption/lossy records
  when the conversion would need any, for pipelines that must not accept
  guessed conversions (#63).

### Changed
- Two previously-silent auto-fixes are now recorded (#63): dropping
  `allowEmptyValue` from a non-query parameter (`x-s2o.lossy`) and
  renaming a colliding operationId (`same` → `same_2`,
  `x-s2o.assumptions`).
- A missing response `description` is now filled with the standard HTTP
  status phrase (`200 → "OK"`, `404 → "Not Found"`, `default → "Default
  response"`; unknown codes stay empty) instead of always an empty
  string — better tool descriptions for LLMs; still recorded in
  `x-s2o.assumptions` (#61).
- `load_spec` no longer silently replaces invalid bytes when fetching a
  spec over http(s): a document that is not valid UTF-8 now fails with a
  labeled `ConversionError` instead of converting with corrupted text
  (#84).

### Fixed
- Library entry points no longer leak exceptions outside the
  `ConversionError` contract (#84): a non-Swagger-2.0 mapping and a
  document that parses to a non-mapping now raise `ConversionError`
  (previously bare `ValueError`), a non-UTF-8 local file raises a
  labeled `ConversionError` (previously a raw `UnicodeDecodeError`
  without the file name), and `spec_has_soap` returns `False` on
  malformed input instead of crashing.
- `convert_swagger`, `convert_wsdl`, and `build_spec` now validate the
  `openapi_version` argument: unsupported values (`"3.2"`, `"2.0"`) raise
  `ConversionError` naming the accepted forms instead of silently
  emitting a 3.0 document, and numeric `3.0`/`3.1` (with or without a
  patch suffix) are accepted instead of crashing; `convert_wsdl` rejects
  a bad version before fetching/parsing the WSDL (#83).
- Deep local `$ref`s that address an arbitrary document location
  (`#/paths/.../responses/200/schema/...`,
  `#/definitions/Foo/properties/bar`) no longer dangle after the
  conversion moves their target: the referenced source subtree is
  resolved and inlined (with all schema fixups applied), recorded in
  `x-s2o.assumptions`; cyclic or unresolvable pointers become `{}` and
  are recorded in `x-s2o.lossy` (#75).
- `collectionFormat` inside an Items Object (legal in Swagger 2.0 for
  nested-array serialization) no longer leaks into the OpenAPI 3 schema:
  it is preserved as `x-collectionFormat` and recorded in `x-s2o.lossy`,
  since OpenAPI 3 has no serialization keyword inside schemas (#76).
- A `default` that does not satisfy its own schema is now coerced when
  trivially convertible (`"1"` → `1` for integer/number, `"false"` →
  `false` for boolean, numeric/bool → string for string type; recorded
  in `x-s2o.assumptions`) and dropped otherwise — including a string
  default that violates the schema's own `pattern` (recorded in
  `x-s2o.lossy`) (#73).
- Percent-encoded `$ref` tokens (`#/definitions/Ref%20(of%20Bundle)`) are
  decoded (RFC 6901 + percent-encoding) before the sanitized-key lookup,
  so the rewritten reference points at the actual component instead of
  dangling (#74).
- Component keys are now sanitized across every namespace, not just
  schemas (#72): `securityDefinitions` / global `parameters` / global
  `responses` names with invalid characters (`Basic Auth`,
  `filter[code]`) map to valid `securitySchemes`/`parameters`/
  `requestBodies`/`responses` keys, with every reference rewritten —
  security requirement objects (root and operation) and `$ref`s.
  Renames recorded in `x-s2o.assumptions`.
- Siblings next to a schema `$ref` (commonly a `description`) are now
  wrapped in `allOf` so OpenAPI 3.0 consumers no longer silently ignore
  them; recorded in `x-s2o.assumptions` (#67).
- A templated `host`/`basePath` (`{region}.example.com`) now declares the
  template names under `servers[].variables` (empty defaults, recorded in
  `x-s2o.assumptions`) instead of emitting an unusable URL with undeclared
  variables (#65).
- A `$ref` to a global `formData` parameter is now dereferenced and merged
  into the operation's form `requestBody`, and the global entry (which has
  no standalone OpenAPI 3 equivalent) is dropped from components with an
  `x-s2o` record — previously the output carried an invalid
  `in: formData` component and an unresolved `$ref` (#59).
- A string-valued `consumes`/`produces` (a spec violation seen in the
  wild) is now wrapped into a list instead of being split into characters,
  which silently corrupted the `content` map keys; recorded in
  `x-s2o.assumptions` (#55).
- Five more non-conformant inputs are normalized instead of passing
  through as invalid OpenAPI 3 (#57): a draft-4 boolean `required` on a
  property is hoisted into the parent's `required` array; a literal
  `type: 'null'` becomes `nullable: true`; a tuple-style `items` array is
  collapsed to a single schema (or `anyOf`); a non-string `info.title`/
  `info.version` is coerced to a string; and `null` values on structural
  fields are stripped up front (`x-` extensions and `example`/`default`/
  `enum` data are preserved). All recorded in `x-s2o.assumptions`.

## [0.2.2] - 2026-07-17

### Fixed
- Library entry points reject the wrong input type with a clear error
  instead of crashing downstream (#50): `convert_swagger` requires a
  mapping and `convert_wsdl` a path/URL string — a mismatch raises
  `ConversionError` naming the expected and actual type; `is_swagger2`
  returns `False` for a non-mapping.
- Format errors are now traceable (#48): JSON/YAML syntax errors are
  prefixed with the source file path (in addition to the line/column the
  parser reports), and a malformed WSDL is pre-parsed so the error points
  at the XML syntax location (`invalid XML in <file>: … line X, column Y`)
  instead of a misleading "no convertible operations" or a cryptic
  zeep-internal message.

## [0.2.1] - 2026-07-14

### Added
- `ConversionError` (exported; subclass of `ValueError`) raised when a
  source cannot be faithfully converted, so failures surface as a clear
  message (CLI: `error: …`, exit 2) instead of a silent skip (#25).

### Changed
- Every converted parameter now carries an explicit boolean `required`
  (`false` when the Swagger source omitted it, `true` for path
  parameters), instead of relying on the OpenAPI default — same meaning,
  unambiguous output (#29).

### Fixed
- Swagger `definitions` names with invalid characters (spaces, slashes)
  are sanitized to valid OpenAPI 3 component keys, with every `$ref`
  rewritten to match and collisions deduped; renames recorded in
  `x-s2o.assumptions` (#33).
- More context-required OpenAPI 3 rules enforced (#31): a path key without
  a leading `/` is prefixed (Swagger paths and WSDL `--base-path`); a `tag`
  without a `name` is dropped; a `type: array` schema without `items` gets
  `items: {}` (parameters, form fields, and definitions). Recorded in
  `x-s2o`.
- Unconvertible input now fails loudly instead of being dropped silently
  (#25): an XSD element with an unresolvable type (which would drop a
  required schema property) and a WSDL that yields zero convertible SOAP
  operations both raise `ConversionError` with a message explaining what
  could not be converted.
- Recoverable Swagger defaults/skips that were previously applied silently
  are now recorded (#27): a synthesized `200` response for an operation
  with no `responses`, a missing response `description`, a dropped
  non-object path item or parameter — all recorded in `x-s2o`. An
  `xsd:any` now emits `additionalProperties: true` (with a warning)
  instead of being dropped.
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

[Unreleased]: https://github.com/Seo-yul/spec2openapi/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/Seo-yul/spec2openapi/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Seo-yul/spec2openapi/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Seo-yul/spec2openapi/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Seo-yul/spec2openapi/releases/tag/v0.1.0
