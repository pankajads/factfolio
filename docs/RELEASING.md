# Releasing

How a version actually gets from a merged PR to something someone can
`pip install` or download and run. One-time setup happens once; cutting a
release is three steps every time.

## One-time setup (do this once, right after pushing the repo to GitHub)

### 1. Branch protection + auto-merge

Moved to [`CONTRIBUTING.md`](../CONTRIBUTING.md) — it covers the full merge
policy (who can merge, PR + CI required, auto-merge for the maintainer's
own PRs) in one place rather than splitting it from the branch-protection
commands that implement it.

### 2. Register PyPI trusted publishing (no API token needed)

No manual upload needed first — PyPI's **pending publisher** flow lets you
claim the `factfolio` project name via GitHub Actions before it exists on
PyPI at all. Go to
[pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/)
(account needs 2FA enabled) and add:

| Field | Value |
|---|---|
| PyPI Project Name | `factfolio` |
| Owner | `pankajads` |
| Repository name | `factfolio` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The *first* successful run of `release.yml`'s `publish-pypi` job from that
exact repo/workflow/environment creates the project and publishes to it —
nothing publishes just from registering this.

This is what `id-token: write` + the `environment: pypi` in
`release.yml`'s `publish-pypi` job authenticates against — no secret to
generate, store, or rotate.

## Cutting a release (every time)

1. **Bump the version.** Edit `version = "..."` in `pyproject.toml`. This
   is the single source of truth — `factfolio --version`, the PyPI package,
   and the release-workflow's own version gate all read it (or a mirror of
   it — see below).
2. **Merge that as a normal PR** — same `ci.yml` gate as everything else.
3. **Tag and push**, matching the version exactly (with a `v` prefix):
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

That tag push triggers `release.yml`, which:

1. **`verify-version`** — fails the whole run immediately if the tag
   doesn't match `pyproject.toml`'s version (a bumped-and-forgot-to-tag or
   tagged-and-forgot-to-bump mistake stops here, before anything publishes).
2. **`test`** — the same lint + pytest gate as `ci.yml`, run again here on
   principle: a tag can technically point at any commit, not only one that
   went through a PR.
3. **`publish-pypi`** and **`build-executables`** run in parallel once both
   of the above pass — the wheel/sdist go to PyPI; three PyInstaller
   binaries (Linux/macOS/Windows) get built, each smoke-tested in its own
   job (`--version`, then a real `init` in a scratch directory) before
   being accepted, not just built.
4. **`github-release`** only runs if every job above succeeded, and
   attaches all three executables to a GitHub Release for that tag.

If anything fails, nothing partial gets published — there's no step where
e.g. PyPI has v0.2.0 but the GitHub Release doesn't, or vice versa, because
`github-release` depends on both.

## Why an executable can still need internet + a login

Skimming the artifacts you'll see three ~130MB+ single-file binaries (built
with PyInstaller, includes the interpreter and every dependency — pandas,
numpy, scipy, streamlit, plotly, and the rest). They remove the need to
install Python or clone the repo. They do **not** remove the need to
install and authenticate the separate `claude` CLI (`claude login` or
`ANTHROPIC_API_KEY`) — `factfolio report` and `factfolio chat` shell out to
it via `claude_agent_sdk`, and no packaging choice here changes that. Keep
that expectation explicit wherever these binaries are advertised.

## What's validated vs. assumed

The PyInstaller build (`packaging/factfolio.spec`) was built and run
end-to-end on macOS during development of this pipeline — CLI, `status`
against a real portfolio, and the Streamlit dashboard (`bootstrap.run()` in
the frozen branch of `cmd_dashboard`, since `sys.executable -m streamlit`
can't work inside a frozen binary) all confirmed working. Linux and Windows
are **not** locally validated — only what the matrix build's own smoke test
in `release.yml` confirms (`--version` + `init`, not the full app, and
specifically not the dashboard, which is the most PyInstaller-fragile
piece). Watch the first real release's `build-executables` job closely, and
manually try `factfolio dashboard` on a Linux/Windows machine soon after —
don't assume the smoke test caught everything a human would notice.
