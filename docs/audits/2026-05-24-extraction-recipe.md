# PersonaLab fresh-repo extraction — recipe

**Date:** 2026-05-24 · **Source branch:** `personalab/publish` (this branch) · **Mode:** checklist for the next session — concrete commands, not a re-derivation.

This is the recipe for lifting `personalab/` + the 9 validation audits
out of the careerops monorepo into a fresh standalone repository.
**Don't run any of this in this session.** This file exists so the
extraction session has the exact invocations + verification steps.

---

## 0. Open question — repo name (decide before the push)

**The package is currently called `personalab`. That name is likely taken on GitHub and PyPI, and is generic.** Pick a final name before creating the destination repo. Criteria:

- Pronounceable, one word, available on both `github.com/nick-ruzicka/<name>` and `pypi.org/project/<name>/`.
- Hints at the moat: cross-persona, friction, UX, or "many users at once."
- Short enough to type in a `pip install` without sighing.

Possible directions (not recommendations — Nick's call):

| Direction | Example flavor |
|---|---|
| Persona-forward | `persona-rig`, `persona-suite`, `personagrid` |
| Friction-forward | `frictionlab`, `frictionscope`, `uxfriction` |
| N-personas-forward | `octouser`, `panellab` (8 personas = jury / panel) |
| Cross-persona-forward | `crosspersona`, `cohortqa` |
| Keep PersonaLab and live with namespacing | `personalab-ai`, `personalab-qa` |

**Action for the extraction session:**

1. Pick the name. Update every `name = "personalab"` in `pyproject.toml`, every `import personalab.*` reference, every `python3 -m personalab.core.orchestrator` shell invocation, and every prose mention.
2. **Decide whether to rename the package directory too** (`personalab/` → `<newname>/`). If yes, add `--path-rename personalab/:<newname>/` to the filter-repo call in step 2.
3. If keeping the directory as `personalab/` but renaming only the project on PyPI, leave the path-rename out.

The recipe below assumes you've decided. Substitute `<newname>` everywhere it appears. If you're keeping the directory as `personalab`, the directory-rename steps are no-ops.

---

## 1. Tooling prereq

```bash
# git filter-repo is the modern, recommended tool (replaces the
# deprecated git filter-branch). Install via:
brew install git-filter-repo
# or:
python3 -m pip install git-filter-repo
```

Verify:

```bash
git filter-repo --version
```

---

## 2. The extraction itself

`git filter-repo` refuses to run on a repo with existing remotes by
default (safety feature). Clone a fresh copy first.

```bash
# 2.1 — clone the source repo to a SCRATCH location (we will rewrite
# its history; don't reuse this clone for anything else).
SRC=/tmp/personalab-extract
rm -rf "$SRC"
git clone --no-local --branch personalab/publish \
  https://github.com/nick-ruzicka/nick-career-ops.git "$SRC"

# 2.2 — enter the scratch clone and do the extraction.
cd "$SRC"

# Keep only the paths we want, AND rewrite their locations as we go.
# Single invocation — filter-repo applies all --path / --path-rename
# rules together.
git filter-repo \
  --path personalab/ \
  --path .github/workflows/personalab-ci.yml \
  --path docs/audits/2026-05-22-personalab-rereview.md \
  --path docs/audits/2026-05-23-personalab-depth-design.md \
  --path docs/audits/2026-05-23-personalab-dispatch-test.md \
  --path docs/audits/2026-05-23-personalab-forge-pass2.md \
  --path docs/audits/2026-05-23-personalab-phase-b.md \
  --path docs/audits/2026-05-23-personalab-rework.md \
  --path docs/audits/2026-05-24-personalab-phase-c.md \
  --path docs/audits/2026-05-24-personalab-prepublish.md \
  --path docs/audits/2026-05-24-personalab-publish-smoke-gates.md \
  --path docs/audits/2026-05-24-extraction-recipe.md \
  --path-rename personalab/pyproject.toml:pyproject.toml \
  --path-rename personalab/README.md:README.md \
  --path-rename .github/workflows/personalab-ci.yml:.github/workflows/ci.yml
```

**What this does:**

- `--path <p>`: keep only files matching these paths through the rewritten history.
- `--path-rename A:B`: rewrite the path A to B across all commits.
- After this runs, `$SRC` contains a rewritten history where the only commits touching the kept paths remain, and the kept paths are at their final locations.

**Result tree should look like:**

```
README.md                                  # was personalab/README.md
pyproject.toml                             # was personalab/pyproject.toml
personalab/                                # the package — unchanged path
├── __init__.py
├── core/
├── personas/
├── schemas/
├── tests/
└── examples/quickstart/
.github/workflows/ci.yml                   # was .github/workflows/personalab-ci.yml
docs/audits/                               # 10 audit docs preserved
├── 2026-05-22-personalab-rereview.md
├── 2026-05-23-personalab-depth-design.md
├── 2026-05-23-personalab-dispatch-test.md
├── 2026-05-23-personalab-forge-pass2.md
├── 2026-05-23-personalab-phase-b.md
├── 2026-05-23-personalab-rework.md
├── 2026-05-24-personalab-phase-c.md
├── 2026-05-24-personalab-prepublish.md
├── 2026-05-24-personalab-publish-smoke-gates.md
└── 2026-05-24-extraction-recipe.md
```

**If renaming the package directory** (only if `<newname>` ≠ `personalab`), add this rename to the filter-repo invocation:

```bash
  --path-rename personalab/:<newname>/
```

---

## 3. Post-filter cleanup — paths that need fixing

After step 2 the directory tree is right but **two files have stale references that filter-repo's path-renames can't catch** (they're inside file contents, not paths). Fix in a new commit at the top of the rewritten history.

### 3.1 — `pyproject.toml` package layout

The file currently has (because it lived inside `personalab/`):

```toml
packages = [
    "core",
    "personas",
    "schemas",
]
package-dir = { "" = "." }
```

Now that `pyproject.toml` is at the repo root and `personalab/` (the
package) is a subdirectory, change to:

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["personalab*"]
```

Or if renaming the package directory in §2:

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["<newname>*"]
```

Python equivalent (run from the new repo root):

```bash
python3 - <<'PY'
import re
text = open("pyproject.toml").read()
old = '''packages = [
    "core",
    "personas",
    "schemas",
]
package-dir = { "" = "." }'''
new = '''[tool.setuptools.packages.find]
where = ["."]
include = ["personalab*"]'''
text = text.replace(old, new)
open("pyproject.toml", "w").write(text)
PY
```

Also update `[project.scripts]` if you renamed the package:

```toml
# from:
personalab = "personalab.core.orchestrator:main"
# to (if renamed):
<newname> = "<newname>.core.orchestrator:main"
```

### 3.2 — `.github/workflows/ci.yml` path filter

The workflow currently filters to `personalab/**` so it doesn't fire
on unrelated careerops PRs. In a standalone repo every commit IS
PersonaLab — the path filter is unnecessary and wrong (it'd miss
top-level docs / pyproject changes). Remove it:

```bash
python3 - <<'PY'
text = open(".github/workflows/ci.yml").read()
old = '''on:
  push:
    branches: [main]
    paths:
      - "personalab/**"
      - ".github/workflows/personalab-ci.yml"
  pull_request:
    paths:
      - "personalab/**"
      - ".github/workflows/personalab-ci.yml"'''
new = '''on:
  push:
    branches: [main]
  pull_request:'''
text = text.replace(old, new)
open(".github/workflows/ci.yml", "w").write(text)
PY
```

If the package directory was renamed, also fix the pytest invocation:

```bash
sed -i.bak 's|personalab/tests/|<newname>/tests/|g' .github/workflows/ci.yml
rm .github/workflows/ci.yml.bak
```

### 3.3 — README link rewriting

`personalab/README.md` (now at root) has links written for its old location:

```
../docs/audits/2026-05-23-personalab-forge-pass2.md   →  docs/audits/2026-05-23-personalab-forge-pass2.md
../docs/audits/2026-05-23-personalab-depth-design.md  →  docs/audits/2026-05-23-personalab-depth-design.md
../docs/audits/2026-05-23-personalab-dispatch-test.md →  docs/audits/2026-05-23-personalab-dispatch-test.md
../docs/audits/2026-05-23-personalab-phase-b.md       →  docs/audits/2026-05-23-personalab-phase-b.md
../docs/audits/2026-05-24-personalab-phase-c.md       →  docs/audits/2026-05-24-personalab-phase-c.md
../docs/audits/2026-05-24-personalab-publish-smoke-gates.md → docs/audits/2026-05-24-personalab-publish-smoke-gates.md
core/behavior.py                                       →  personalab/core/behavior.py
tests/test_injection_resistance.py                    →  personalab/tests/test_injection_resistance.py
examples/quickstart/                                   →  personalab/examples/quickstart/
core/runner.py, core/analyzer.py, core/scenario_runner.py, core/replayer.py, core/synthesizer.py, core/orchestrator.py
                                                       →  personalab/core/<file>
```

Single sed pass (from the new repo root):

```bash
sed -i.bak \
  -e 's|\(\]\)(../docs/audits/|\1(docs/audits/|g' \
  -e 's|\(\]\)(core/|\1(personalab/core/|g' \
  -e 's|\(\]\)(tests/|\1(personalab/tests/|g' \
  -e 's|\(\]\)(examples/|\1(personalab/examples/|g' \
  README.md
rm README.md.bak
```

If renaming the package: substitute `personalab/` → `<newname>/` in the same pass.

Also fix any `import personalab` or `python3 -m personalab.*` strings if the package directory was renamed:

```bash
# Only if renamed:
sed -i.bak \
  -e 's|import personalab\.|import <newname>.|g' \
  -e 's|python3 -m personalab\.|python3 -m <newname>.|g' \
  -e 's|`personalab\.|`<newname>.|g' \
  README.md docs/audits/*.md
find . -name '*.bak' -delete
```

### 3.4 — Audit-doc broken links

The audits reference files that DON'T travel to the extracted repo:

- `forge-qa/` — careerops test fixture, intentionally excluded.
- `chariot-qa/` — same.
- `qa/` — careerops-specific.

These references appear in 3-4 audit docs as evidence pointers ("session JSONL at forge-qa/_depth/runs/..."). **They WILL be broken links in the standalone repo.** Two options:

- **(a) Leave them.** The audits are historical record. A broken link to `forge-qa/_depth/runs/cautious-first-timer-20260523T194621Z.jsonl` is still useful prose. Recommend this.
- **(b) Rewrite to point at a frozen `validation-history/` directory.** Copy a curated subset of forge-qa/chariot-qa JSONLs/reports into `docs/validation-history/` before the extraction. More effort; only worth it if you anticipate someone trying to reproduce the validation runs from the audits.

Recommend (a) unless someone files a confused-user issue.

### 3.5 — Commit the cleanup

```bash
git add -A
git commit -m "chore: post-extraction path fixes — pyproject layout, CI filter removal, README links"
```

---

## 4. Verification checklist (must hold before pushing)

Run from the new repo root.

### 4.1 — files that MUST be present

```bash
test -f README.md                                  || echo "MISSING: README.md"
test -f pyproject.toml                             || echo "MISSING: pyproject.toml"
test -f .github/workflows/ci.yml                   || echo "MISSING: .github/workflows/ci.yml"
test -d personalab/core                            || echo "MISSING: personalab/core/"
test -d personalab/personas                        || echo "MISSING: personalab/personas/"
test -d personalab/schemas                         || echo "MISSING: personalab/schemas/"
test -d personalab/tests                           || echo "MISSING: personalab/tests/"
test -d personalab/examples/quickstart             || echo "MISSING: personalab/examples/quickstart/"
ls personalab/personas/*.yaml | wc -l  # expect 8
ls personalab/core/*.py | wc -l        # expect 9 (8 modules + __init__.py)
ls docs/audits/2026-05-2*-personalab-*.md | wc -l  # expect 9
test -f docs/audits/2026-05-24-extraction-recipe.md || echo "MISSING: this recipe"
```

### 4.2 — files that MUST NOT be present (careerops bleed-through)

These are the canonical careerops paths that **should NOT** exist in the
extracted repo. If any of these show up, the filter-repo invocation in
step 2 captured too much.

```bash
# Personal data / PII (these contain real CRM-style data, NEVER ship)
test ! -f cv.md                            || echo "FOUND PII: cv.md"
test ! -f config/profile.yml               || echo "FOUND PII: config/profile.yml"
test ! -f config/user-context.yaml         || echo "FOUND PII: config/user-context.yaml"
test ! -f data/applications.md             || echo "FOUND PII: data/applications.md"
test ! -f data/score-overrides.json        || echo "FOUND PII: data/score-overrides.json"
test ! -f data/seen-urls.json              || echo "FOUND: data/seen-urls.json"
test ! -f data/enrichments.json            || echo "FOUND: data/enrichments.json"
test ! -d interview-prep                   || echo "FOUND: interview-prep/"
test ! -d autoapply                        || echo "FOUND: autoapply/"

# Careerops scoring engine (the chore commit's bleed-through)
test ! -f scripts/lib/comp-parse.mjs        || echo "FOUND: scripts/lib/comp-parse.mjs"
test ! -f scripts/lib/scoring-layer.mjs     || echo "FOUND: scripts/lib/scoring-layer.mjs"
test ! -f scripts/scan-jobs.mjs             || echo "FOUND: scripts/scan-jobs.mjs"
test ! -f scripts/enrich-roles.mjs          || echo "FOUND: scripts/enrich-roles.mjs"
test ! -f scripts/merge-tracker.mjs         || echo "FOUND: scripts/merge-tracker.mjs"

# Careerops dashboard / app
test ! -d dashboard-web                     || echo "FOUND: dashboard-web/"
test ! -d dashboard                         || echo "FOUND: dashboard/"
test ! -f portals.yml                       || echo "FOUND: portals.yml"

# Non-personalab audits / work logs
test ! -f docs/audits/2026-05-22-fde-sourcing-plan.md   || echo "FOUND: fde-sourcing-plan"
test ! -f docs/audits/2026-05-22-extractability.md      || echo "FOUND: extractability"
test ! -f docs/audits/2026-05-18-codebase-review.md     || echo "FOUND: 2026-05-18 codebase audits"
ls WORK_LOG*.md 2>/dev/null | head -1 | grep -q . && echo "FOUND: WORK_LOG*.md"
ls MORNING_REPORT*.md 2>/dev/null | head -1 | grep -q . && echo "FOUND: MORNING_REPORT*.md"

# Local-machine test fixtures from validation runs (should NOT travel)
test ! -d forge-qa                          || echo "FOUND: forge-qa/ (test fixture should stay in careerops)"
test ! -d chariot-qa                        || echo "FOUND: chariot-qa/ (test fixture should stay in careerops)"
test ! -d qa                                || echo "FOUND: qa/ (careerops qa)"

# Vendored reference repos (gitignored in careerops; should not be in history)
test ! -d hebbia-signal-engine-reference    || echo "FOUND: hebbia-signal-engine-reference/"
test ! -d forge                             || echo "FOUND: forge/ (gitignored ref repo)"
```

If any of these fire, the filter-repo `--path` list in step 2 needs
narrowing OR the source branch had unintended files (rerun from the
canonical `personalab/publish` tip, not a stale local).

### 4.3 — Package installs and tests pass

```bash
python3 -m pip install -e .[test]
python3 -m playwright install chromium
python3 -m pytest -q
# Expect: 165 passed
```

### 4.4 — CI workflow lints

```bash
# Validate the workflow YAML
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

### 4.5 — README renders (manually verify)

Open `README.md` in a Markdown viewer (or `gh repo view` after push)
and confirm:

- Headline and quickstart links resolve.
- The 5 sections (positioning / vs-plain-Claude / vs-related-tools /
  security / limitations) are present and render correctly.
- Internal links to `docs/audits/...` and `personalab/...` paths
  don't 404 (no `../docs/` left, no bare `core/` left).

### 4.6 — No secrets in history

```bash
# Quick sanity scan
git log --all -p | grep -iE 'sk-ant-|api[_-]?key.*=.*[a-z0-9]{20,}' | head
# Expect: zero matches
```

The framework code reads `ANTHROPIC_API_KEY` from env exclusively, but
worth confirming no test fixture or example accidentally inlined one.

---

## 5. Push to the new repo

Once steps 1-4 all pass:

```bash
# 5.1 — create the GitHub repo (gh CLI is cleanest)
gh repo create nick-ruzicka/<newname> --public \
  --description "Cross-persona UX-friction detector + release-over-release regression harness" \
  --homepage "https://nick-ruzicka.github.io/<newname>"

# 5.2 — point the local extracted history at it
cd "$SRC"
git remote add origin git@github.com:nick-ruzicka/<newname>.git
git branch -M main                # was personalab/publish
git push -u origin main
```

After the push, the GitHub Actions CI should fire automatically. Wait
for it to go green before adding a README badge.

---

## 6. PyPI publish (optional, can wait)

If you want `pip install <newname>` to work:

```bash
python3 -m pip install --upgrade build twine
python3 -m build
python3 -m twine upload dist/*
```

You'll need a PyPI account + `~/.pypirc` token. Test on
`https://test.pypi.org` first via `twine upload --repository testpypi dist/*`.

---

## 7. Post-publish hygiene

- [ ] Add a CI status badge to README.md
- [ ] Add a "Star history" link if you care about social proof
- [ ] File the first GitHub Issue as "Phase D: 4th-codebase validation
      against a complex app" (the deferred Dispatch test)
- [ ] Update `personalab/examples/quickstart/README.md` if it has any
      references that need to become root-relative
- [ ] Mark the careerops `personalab/publish` branch as merged-and-
      archived (no need to keep it active after the extract works)
- [ ] If renamed, update the audit docs' "PersonaLab" prose to the
      new name (or add a heading note: "Formerly known as PersonaLab")

---

## What this recipe does NOT do

- **Doesn't run the extraction.** That's the next session.
- **Doesn't decide the final repo name.** Open question at the top.
- **Doesn't enforce semver or write a CHANGELOG.** `pyproject.toml`
  has `version = "0.1.0"` — leave it there; first release tag can be
  `v0.1.0` on the new repo.
- **Doesn't write a CONTRIBUTING.md.** Maybe worth a follow-up if
  the project picks up contributors; for v0 it's not load-bearing.
- **Doesn't add a LICENSE file.** Add `LICENSE` (MIT) at the new
  root before the first push — the `pyproject.toml` declares MIT but
  GitHub looks for the file separately for the repo's license badge.

---

## Cost of running this recipe

`git filter-repo` is local-only ($0). All sed / Python rewrites are
local ($0). One CI run on first push to verify ($0 for public repos).
Optional `twine upload` to PyPI ($0). **Total: $0.** The recipe is
deliberately API-call-free so it can be re-run if anything goes wrong.

---

## Source state this recipe was written against

- **Branch:** `personalab/publish`
- **Tip:** `eb09b64` (publish-prep — README, pyproject.toml, CI, smoke-gate audit)
- **Off:** `5c8de63` (origin/main)
- **Tests:** 165 passing
- **Files in scope:** `personalab/` (entire package), `.github/workflows/personalab-ci.yml`, 9 personalab audits under `docs/audits/`, this recipe.

If `personalab/publish` advances before the extraction session, re-read
the paths in step 2 against the new tip — adding the new ones if
they're in scope.
