# Branching & release model

This project uses a small, predictable Git flow: a stable release branch, an
integration branch, and short-lived issue branches. All merges are
**squash merges**, so history stays linear and each pull request becomes a
single commit.

## Branches

| Branch | Role | Created from | Merges into | Merge method |
|--------|------|--------------|-------------|--------------|
| `main` | **Default branch.** Always the latest release; every commit is a release (`release: vX.Y.Z`). Tags and PyPI publishes happen here. | — | — | — |
| `develop` | Integration branch where day-to-day work lands. | `main` (once) | `main` | squash (release PR) |
| `feature/<issue>-<slug>` | One issue's work (features and fixes). | `develop` | `develop` | squash |
| `hotfix/<issue>-<slug>` | Urgent fix to a released version. | `main` | `main` | squash, then back-merge |

```
main ───────●───────────────●─────────  release: v0.1.0, v0.2.0 …  (each commit = a release)
             \             / \
              \           /   ↘ back-merge (required)
develop ──●─●──●──●────●──●──●─────────  integration branch
           \    /        \  /
   feature/12-…●●(squash) ●  feature/34-…
```

> **`main` is the default branch (what visitors see), but all contributions
> target `develop`.** `main` only ever receives release merges.

## Naming

- Branch names carry the issue number: `feature/123-support-substitution-groups`.
- Every change starts from an issue — open one first (Bug / Feature / Refactor
  templates). Slugs are lowercase kebab-case, 3–5 words.

## feature → develop

1. Branch from `develop` (not the default `main`):
   `git switch develop && git pull && git switch -c feature/123-slug`
2. Open the PR **against `develop`** with `Closes #123` in the body. If GitHub
   pre-selects `main` as the base, change it to `develop`.
3. CI (test matrix + build) must be green.
4. **Squash and merge only.** The squash commit message follows
   [Conventional Commits](https://www.conventionalcommits.org/):
   `feat: support substitution groups (#123)`.
5. The feature branch is deleted after merge.

## develop → main (release)

1. Finalize `CHANGELOG.md` (move `[Unreleased]` to the new version) and bump
   the version in `pyproject.toml` and `src/spec2openapi/__init__.py` — via a
   normal feature PR into `develop`.
2. Open a release PR from `develop` to `main`, titled exactly
   `release: vX.Y.Z`.
3. **Squash and merge**, so `main` gains a single `release: vX.Y.Z` commit.
4. Tag on `main`, publish the GitHub Release, and upload to PyPI.
5. **Back-merge (do not skip):** immediately merge `main` back into `develop`
   so the two branches stay aligned — otherwise the next release PR replays
   already-released changes and conflicts. This is the one place a merge
   commit is allowed on `develop`.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):
`<type>: <subject>` (`feat`, `fix`, `docs`, `test`, `refactor`, `chore`,
`ci`, `perf`). Since PRs are squash-merged, the PR title becomes the commit
message — write it in the same form. Release squash commits are the only
exception: `release: vX.Y.Z`.

## Repository settings

- Default branch: `main`.
- Branch protection on `main` and `develop`: PR required, CI required, no
  force-push.
- Only **Squash and merge** is enabled (merge-commit and rebase are off);
  the back-merge is done locally via the CLI.
- "Automatically delete head branches" is enabled.
