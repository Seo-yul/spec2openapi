## Related Issue

Closes #XXX

## Changes

### `path/to/file.py` — description of the change

| Item | Before | After |
|------|--------|-------|
| ...  | ...    | ...   |

## Breaking Changes

- [ ] This includes a breaking change

(If checked, describe the impact and a migration path)

## Verification

- [ ] `python -m pytest tests/` passes locally
- [ ] Tests added/updated (fixture + assertion; e2e via the mock SOAP server
      if wire behavior changed)
- [ ] The FastMCP guarantee holds: affected fixtures/examples pass
      `spec2openapi validate`
- [ ] No sorting introduced in the emit path (property order = XSD sequence
      order)
- [ ] Core/`[mcp]` dependency split respected (no `fastmcp`/`httpx` imports in
      core modules)
- [ ] `CHANGELOG.md` updated under `Unreleased`
- [ ] Docs updated (`README.md` / `README.ko.md`) if behavior changed
