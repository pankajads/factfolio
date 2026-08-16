# Contributing

## Policy

- **Every change to `main` goes through a pull request that passes CI**
  (`.github/workflows/ci.yml`) — no direct pushes, no exceptions, enforced
  by branch protection (setup below), not just convention.
- **Only the maintainer ([@pankajads](https://github.com/pankajads),
  see [`.github/CODEOWNERS`](.github/CODEOWNERS) and
  [`CONTRIBUTORS.md`](CONTRIBUTORS.md)) can merge.** Anyone can open an
  issue or a PR — genuinely welcome, see the README — but merging a PR you
  didn't author yourself requires repo write access, which isn't handed
  out. Claude Code commits under the maintainer's own git identity, not a
  separate account, so this is a one-person merge policy, not a two-person one.
- **PRs authored by the maintainer auto-merge** the moment CI goes green
  (`.github/workflows/auto-merge.yml`) — no manual "Merge" click needed for
  first-party changes. This does **not** apply to external PRs: the
  workflow only enables auto-merge for PRs opened by `pankajads` from a
  branch in this repo, never from a fork, so an outside contribution can
  never merge itself no matter how green its CI run is.

## One-time repo setup

**Status: done** (applied 2026-08-16). Left here for reference — e.g. if
branch protection ever needs to be recreated on a fork or a renamed repo.
Needs at least one CI run to already exist (so GitHub knows the `test`
check to select) and `gh auth login` as the repo owner:

**1. Require PR + passing CI before anything reaches `main`:**
```bash
gh api repos/pankajads/factfolio/branches/main/protection -X PUT \
  -H "Accept: application/vnd.github+json" \
  -F 'required_status_checks[strict]=true' \
  -f 'required_status_checks[contexts][]=test' \
  -F 'enforce_admins=true' \
  -F 'required_pull_request_reviews[required_approving_review_count]=0' \
  -F 'restrictions=null'
```
(`-F`, not `-f`, for `strict` and `required_approving_review_count` — `-f`
sends a string, and GitHub's schema rejects `"true"`/`"0"` as a boolean/
integer. `-f` is correct for `contexts[]`, a real string array.)
`required_approving_review_count=0` is deliberate: it still forces every
change through a PR (the `required_pull_request_reviews` object being
present at all is what blocks direct pushes — `null` would not), just
without demanding an approval click nobody else is around to give.
`enforce_admins=true` means even the owner can't bypass this by
force-pushing. Equivalent UI path: **Settings → Branches → Add branch
protection rule** → `main` → check **Require a pull request before
merging** (0 approvals) + **Require status checks to pass** (select `test`)
+ **Do not allow bypassing the above settings**.

**2. Allow auto-merge at the repo level** (a prerequisite for
`auto-merge.yml` to be able to call `gh pr merge --auto` at all):
```bash
gh repo edit pankajads/factfolio --enable-auto-merge
```
Equivalent UI path: **Settings → General → Pull Requests → Allow auto-merge**.

## Day to day

```bash
git checkout -b my-change
# ... edit ...
uv run pytest -q && uv run ruff check src/ tests/
git commit -m "..." && git push origin my-change
gh pr create
```
CI runs automatically; if you're the maintainer, `auto-merge.yml` enables
auto-merge on the PR and it merges itself once `test` passes — nothing
further to do. Anyone else's PR waits for the maintainer to review and
merge it by hand.
