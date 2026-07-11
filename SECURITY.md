# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | yes       |

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub
issues.

Report them privately to **devops.reso@gmail.com** (or via GitHub's
"Report a vulnerability" private advisory form if enabled on the repository).
Include:

- A description of the issue and its impact
- Steps or a proof-of-concept to reproduce (a minimal WSDL/Swagger/OpenAPI
  document is ideal)
- Affected version(s) and environment

You will receive an acknowledgement within 72 hours. We will work with you on
a fix and coordinate the disclosure timeline; credit will be given in the
release notes unless you prefer otherwise.

## Scope notes

Areas of particular interest:

- XML parsing of untrusted WSDL/XSD documents (entity expansion, external
  entity resolution via lxml/zeep)
- The reference SOAP bridge (`[mcp]` extra): request forging, credential
  handling (`SPEC2OPENAPI_*` environment variables), TLS verification bypass
- Path handling in the CLI (`convert`, `upgrade`, `validate`, `serve`)

## Hardening notes (converting untrusted documents)

All XML parsing disables DTD loading, entity resolution, and parser-level
network access. Two behaviors remain configurable:

- **Remote imports**: by design, `wsdl:import`/`xsd:import` locations
  referenced by the document are fetched (WSDLs commonly split schemas
  across files). A hostile document can point these at internal hosts
  (SSRF). When converting documents from untrusted sources, pass
  `--forbid-external` (CLI) or `forbid_external=True` (API) — remote
  import fetching is refused while local relative imports keep working.
- **Parser limits**: libxml2 depth/size limits are enforced by default;
  `--huge-tree` lifts them and should only be used for large *trusted*
  WSDLs.
