# Contributing to spec2openapi

Thanks for your interest in contributing. This document explains how to get set
up, what we expect from contributions, and how the project is verified.

## Getting started

```bash
git clone https://github.com/Seo-yul/spec2openapi.git
cd spec2openapi
pip install -e ".[dev]"
python -m pytest tests/
```

All 71 tests should pass before you start. Python 3.10+ is required.

## What to work on

- Check the [issue tracker](https://github.com/Seo-yul/spec2openapi/issues)
  for open bugs and feature requests; issues labeled `good first issue` are a
  good entry point.
- For new features (e.g. supporting an additional legacy spec format or an
  unsupported WSDL/XSD construct), please open an issue first to discuss the
  design before sending a large PR.

## Ground rules

1. **The FastMCP guarantee is the project's contract.** Any spec this library
   emits must pass `spec2openapi validate` — static checks,
   `openapi-spec-validator`, and a real `FastMCP.from_openapi()` round-trip.
   PRs that break this contract will not be merged.
2. **Never invent silently.** When source documents lack information, apply a
   deterministic default and record it (`x-s2o.assumptions` for Swagger,
   skipped-operation reports for WSDL). When something cannot be translated,
   preserve it as an `x-` extension and record it (`x-s2o.lossy`).
3. **Property order is semantic.** For SOAP schemas, `properties` order mirrors
   the XSD sequence order and is used for XML serialization. Do not introduce
   sorting anywhere in the emit path.
4. **Core stays lean.** The core package depends only on `zeep`, `lxml`, and
   `PyYAML`. Anything requiring `fastmcp`/`httpx` belongs behind the `[mcp]`
   extra (`bridge.py`, `server.py`) with lazy imports.

## Adding tests

Every behavior change needs a test. The suite is organized around WSDL/Swagger
fixtures in `tests/fixtures/`:

- Converter behavior: add or extend a fixture and assert on the generated spec
  (`test_convert.py`, `test_advanced.py`, `test_swagger.py`, `test_stress.py`).
- Wire behavior: extend the in-process mock SOAP server
  (`tests/mock_soap_server.py`) and add an end-to-end test that calls the MCP
  tool and asserts on the SOAP round-trip (`test_e2e.py`).
- Compatibility: new `.wsdl` fixtures are automatically included in the
  FastMCP round-trip matrix (`test_fastmcp_compat.py`) for OpenAPI 3.0 and 3.1.

Run everything with:

```bash
python -m pytest tests/ -v
```

## Pull request process

`develop` is the default branch and the target of all contributions;
`main` is reserved for releases.

1. Open (or find) an issue first — every PR should be linked to one.
2. Fork and create a topic branch from `develop`
   (naming convention: `feature/<issue-number>-<short-slug>`).
3. Open the PR against `develop` and reference the issue with `Closes #123`.
   PRs are **squash-merged**, so one PR = one commit in history — keep PRs
   focused; unrelated refactoring belongs in separate PRs.
4. Update documentation (`README.md`, `README.ko.md`) and `CHANGELOG.md`
   (Keep a Changelog format, under `Unreleased`) when behavior changes.
5. Make sure CI is green (tests on Python 3.10–3.13 plus a package build).
6. A maintainer will review; please respond to review comments within a
   reasonable time so the PR does not go stale.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):
`<type>: <subject>` with an imperative, present-tense subject
(`feat: add rpc/literal support`, not `added stuff`). Types: `feat`, `fix`,
`docs`, `test`, `refactor`, `chore`, `ci`, `perf`. Since PRs are squash-merged,
the PR title becomes the commit message — write it in the same format.
Reference issues with `Closes #123` in the PR body.

## Reporting bugs

Use the bug report issue template. The most useful bug reports include a
minimal WSDL/Swagger snippet that reproduces the problem — specs are the
project's test currency, and a failing fixture usually becomes the regression
test.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you are expected to uphold it.

## Security issues

Do not open public issues for security vulnerabilities — see
[SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE).
