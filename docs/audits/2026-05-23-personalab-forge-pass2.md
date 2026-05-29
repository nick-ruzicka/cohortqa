# CohortQA Forge Pass-2 — empirical "does it beat plain Claude Code" gate

_Formerly known as **PersonaLab** — renamed to **CohortQA** at OSS publish (2026-05-28)._

**Date:** 2026-05-23 · **Mode:** empirical (real CohortQA + real Claude analyzer/synthesizer + real plain-Claude-Code baseline + careerops control) · **Branch:** `cohortqa/forge-pass2` (config + this doc only — no edits to `cohortqa/core/`) · **Spend:** ~$1.70 of $4.00 budget.

---

## Verdict

**PROCEED to rework + publish, but with tighter framing than the previous rereview suggested.**

- CohortQA's irreducible value (cross-persona pattern synthesis) **survived empirical contact** with Forge — it found 2-3 cross-persona patterns that a single plain Claude Code browser session structurally could not have found (no single confused user hits both persona-A's and persona-B's friction at once).
- **But** plain Claude Code alone — one browser + a one-paragraph prompt, zero setup — found **10 specific UI/accessibility/validation findings** in 10 minutes, vs. CohortQA's **6 patterns** in ~25 min of setup + 2 min of run. The signal-density per setup-hour favors plain CC for any one-off audit.
- The framework is **complementary**, not a replacement. Right framing: "cross-persona UX-pattern detector that runs as a release-over-release regression harness, alongside a plain Claude Code one-shot pass." Wrong framing: "AI QA platform that replaces designers / Plain CC."
- Synthesizer's English-regex re-prompt and confidence-demoter (`synthesizer.py:390-408` and `:477-530`) **did not fire** on Forge — robustness signal: the framework's bones held up on an unfamiliar codebase without needing its safety nets. Instrumentation_gap rate was **0%** on Forge (not a "gap dump").
- Setup cost is real (25 min wall-clock first-run on Forge) but amortizes cleanly across releases. For a one-off "is my UX bad?" check, plain CC wins; for monthly+ release cadence, CohortQA is the structured tool that exists.

**Recommended action:** Do the 1-2 day rework from the prior rereview (kill synthesizer regex fallback, internalize confidence rules in prompts, add `nav_error` + `data_inconsistency` taxonomy types, fix the `data_density`-used-for-sparse-content labeling error documented in §3.4 below) → publish standalone with the reframed positioning.

**Confidence in the verdict:** moderate. This is a borderline call. A FOLD-BACK case is articulable (see §6) and the data isn't lopsided enough to dismiss it.

---

## 1. Setup — what it took to point CohortQA at an unfamiliar codebase

| Phase | Wall-clock | Notes |
|---|---:|---|
| Find Forge dir + read README + docker-compose + .env.example | ~5 min | Real grep + Read; manageable. |
| Verify Forge running (it already was on `:8090`) | ~1 min | Free — user had Flask + Postgres + Redis up via `api/server.py`. |
| Install `playwright` + chromium browser | ~90 sec | Did not have playwright Python module locally; `pip install playwright && python3 -m playwright install chromium` ran clean. ~92 MB chromium download. |
| Inspect Forge's live frontend (curl + grep `/`, `/publish.html`, `/my-tools.html`, `/skills.html`) | ~5 min | Vanilla HTML served from `frontend/`; routes obvious from grep. |
| Write `forge-qa/app.yaml` (4 routes, 16 actions, 9 friction signals) | ~7 min | Templated from `qa/app.yaml`; selectors are CSS class names from the served HTML. |
| Write 2 personas (`tool-submitter`, `tool-browser`) | ~4 min | Mirrored the schema from `qa/personas/`. Skipped scenarios (orchestrator runs fine without). |
| Validate via `python3 -m cohortqa.core.persona_schema` | ~30 sec | Passed first try. |
| **Total Phase-1 setup** | **~25 min** | |

**Honest comparison:** a thoughtful manual exploration of Forge by a human (load `/`, find publish, walk the form, check my-tools) takes ~10-15 min. CohortQA setup is **~1.5× a single manual pass.** Setup amortizes only if CohortQA is re-run regularly; for a one-off check, the math doesn't favor it.

**README→UI discrepancy as a setup-side finding:** Forge's README describes a "5-step submission flow" (Basics/Inputs/Prompt/Governance/Review). The deployed UI at `/publish.html` is a **single-page form** with paste/upload/GitHub mode pills + metadata fields + Publish. A first-time user reading the README and walking the UI would get confused immediately. CohortQA's persona file captures this in `meta_attitude`; the persona definition itself encodes the expectation gap.

**Non-destructive constraint** satisfied by: the `click_publish` action is declared with `side_effects: ["writes:postgres.tools", "writes:celery.review_pipeline"]`. CohortQA's `behavior.is_protected_action` treats any action whose side_effects contain a `writes:` prefix as **intent-logged but never clicked** — personas express the intent to submit without mutating Forge's database. Verified: tool-submitter's session log shows 1 visible action (the publish mode pills) and zero protected-action clicks.

---

## 2. Phase-2 result — CohortQA on Forge

**Run details:** `python3 -m cohortqa.core.orchestrator --app forge-qa/app.yaml --parallel 2`. Elapsed: **126.0s.** Cost: ~$0.30.

**Sessions:**
- `tool-browser`: 4 routes traversed, **0 actions clicked**. Persona ricocheted across `/ → /publish.html → /my-tools.html → /skills.html` in ~1.2s. The "0 actions" is the **vanilla-HTML-instrumentation delta** — Forge's HTML lacks `data-action` hooks that the framework expects (careerops's polish-1 work added these).
- `tool-submitter`: 4 routes, **1 action clicked** (a publish mode pill).

**Synthesis: 6 patterns** (severity / personas affected):

| # | Type | Severity | Personas | Pattern |
|---|---|---|---|---|
| 1 | `empty_state` | high | both | `/my-tools.html` unrecoverable — 188 chars body, 0 actions, no recovery CTA |
| 2 | `data_density` | medium→high | both | Homepage at `/` has 68 chars body, no `.c-card`, no `.cat-pill` (LABELING ERROR — should be `empty_state`, see §3.4) |
| 3 | `missing_action` | high | tool-browser | No install affordance anywhere on homepage |
| 4 | `navigation` | medium | both | Catalog content lives at `/skills.html` with no signposting from `/` (NAMING GAP) |
| 5 | `missing_action` | high (medium-confidence) | tool-submitter | No Preview/Save Draft before irreversible Publish |
| 6 | `missing_action` | medium (medium-confidence) | tool-submitter | No post-submit status / review-pipeline visibility |

**Synthesizer patches:** `synthesizer.py:390-408` (English-regex re-prompt fallback) did **not** fire. `synthesizer.py:477-530` (confidence demoter) did **not** fire. The framework ran without invoking its safety nets — that is itself the robustness signal the prior rereview asked us to measure.

---

## 3. Phase-3 baseline — plain Claude Code with a browser

**Run details:** dispatched a fresh `general-purpose` subagent with NO CohortQA context, NO persona machinery — just `mcp__claude-in-chrome` browser tools and this one-paragraph prompt:

> "Drive http://localhost:8090 with a browser as a confused first-time user trying to submit a tool. Report every point of friction, confusion, or breakage you hit."

**Cost:** ~$0.30 (51,990 tokens, 17 tool uses, 144s elapsed).

**10 specific findings (numbered as returned):**

1. **Two parallel submit flows with different vocabularies.** Apps use `/publish.html` titled "Publish to Forge"; Skills use a modal titled "Submit a Skill" from `/skills.html`. No disambiguating top-level "Submit" entry. *(Partial overlap with CohortQA #4.)*
2. **Inconsistent nav label "My Forge" vs "My submissions"** across pages — same `/my-tools.html` link rendered with two different labels. *(Unique to plain CC.)*
3. **Almost nothing on the publish form is `required` — including the app body itself.** Only `Your email` is required. *(Unique. Forge bug.)*
4. **Publish button is `type=submit` but there is no surrounding `<form>` element.** Enter doesn't submit, no native validation, no `[role=alert]` error region. *(Unique. Accessibility + keyboard.)*
5. **Mode pills look like tabs but aren't real ARIA tabs** — no `role=tab`, no `aria-selected`, no `role=tabpanel`. *(Unique. Accessibility.)*
6. **Icon (emoji) field has no placeholder, no example, no picker.** *(Unique.)*
7. **No required-source enforcement across the three modes.** Publish button stays enabled even with empty source. *(Unique. Forge bug.)*
8. **Anonymous identity model is hidden and confusing.** *(Unique.)*
9. **Catalog cards have no obvious preview affordance** — "Install" looks like the only way to see what a tool is. *(Partial overlap with CohortQA #5 on a different surface.)*
10. **Footer keybinding hints reference shortcuts that don't match the page model** ("I to install" with no card selection indicator). *(Unique.)*

---

## 4. The critical comparison

### 4.1 Findings overlap

| Finding | CohortQA | Plain CC | Notes |
|---|:---:|:---:|---|
| `/my-tools.html` empty / dead-end | ✓ #1 (high, cross-persona) | mentioned as "what worked smoothly" | **DISAGREEMENT.** CohortQA flagged it as a high-severity dead-end; plain CC praised its "Browse apps →" CTA. CohortQA's persona-sensitivity framing matters here — `tool-browser` has `friction_sensitivities: [empty_state]` declared, so the analyzer correctly elevated it. Plain CC, unprimed, saw an empty state with a CTA and called it acceptable. |
| Homepage missing catalog cards | ✓ #2-#3 | not explicit | CohortQA found this; plain CC navigated past `/` and landed cards on `/skills.html`. |
| Tools-vs-Skills / naming gap | ✓ #4 | ✓ #1 (different angle) | Overlap. CohortQA framed it as "catalog at `/skills.html` not signposted"; plain CC framed it as "two parallel submit flows with different vocab." Same underlying issue. |
| No preview before publish | ✓ #5 | ✓ #9 (catalog, not publish) | Different surfaces. CohortQA caught publish-side; plain CC caught catalog-card-side. |
| Post-submit status | ✓ #6 | — | **Unique to CohortQA.** |
| Required-ness mismatch | — | ✓ #3 | **Unique to plain CC. Forge bug.** |
| `type=submit` without `<form>` | — | ✓ #4 | **Unique to plain CC.** |
| ARIA tabs missing | — | ✓ #5 | **Unique to plain CC.** |
| Required-source enforcement | — | ✓ #7 | **Unique to plain CC. Forge bug.** |
| Identity model | — | ✓ #8 | **Unique to plain CC.** |
| Keybinding shortcut mismatch | — | ✓ #10 | **Unique to plain CC.** |

**Count:** CohortQA unique = **3** (the cross-persona patterns + post-submit). Plain CC unique = **7** (the specific UI/accessibility/validation findings). Overlap = **2-3**.

### 4.2 The cross-persona claim

The prior rereview claimed CohortQA's irreducible value is **cross-persona pattern synthesis over real Playwright telemetry**. Empirically tested on Forge:

- Pattern #1 (`/my-tools.html` empty state): both tool-browser AND tool-submitter hit it. The synthesizer's clustering correctly identified this as a CROSS-persona pattern, not a per-persona finding. **Plain CC, as a single confused user, structurally could not surface this** without being told to "imagine you're 5 different users" — and that prompt-engineering effort is itself setup.
- Pattern #4 (catalog naming gap): both personas drifted across `/ → /publish → /my-tools → /skills`. **The drift pattern itself was the finding** — neither persona alone characterized it well; the cross-persona telemetry made it visible.

So the cross-persona claim is **empirically supported**, but the magnitude is modest (~2 patterns out of 6).

### 4.3 The "what plain CC missed" question

Plain CC missed:
- The cross-persona patterns (above)
- The "post-submit status" finding (didn't dwell on what-happens-after-publish)
- The behavioral framing (CohortQA's tool-browser persona is declared as low-patience, high-rejection-threshold; the analyzer elevates findings against that filter. Plain CC has no encoded user model — it's just "confused first-time user.")

Plain CC caught:
- 7 specific UI/accessibility/validation findings CohortQA didn't surface — including 2 genuine Forge bugs (publish form has no required-source enforcement; type=submit with no `<form>`).
- More HTML-level specificity (selectors, ARIA roles, validation hints).
- Faster signal-density per minute of work.

### 4.4 Labeling errors — the `data_density` misuse

CohortQA's #2 pattern was labeled `data_density` ("page renders so much information at once the persona can't decide where to start") for a homepage with **68 chars of body**. That's the OPPOSITE of data density — it's sparse. The correct label is `empty_state`. The analyzer made a wrong-type call on both personas independently, and the synthesizer didn't catch the mislabel.

This is **2 out of 8 events with wrong-type labels (25% type-labeling error rate)**. The events themselves are real friction (the homepage IS sparse); the categorical assignment is wrong. The fix is straightforward — prompt engineering on the analyzer's friction-taxonomy block (`analyzer.py:150-212`) to disambiguate sparse-content from data-overload. Not architectural.

---

## 5. Phase-4 control — CohortQA on careerops (tuned home turf)

**Run details:** `python3 -m cohortqa.core.orchestrator --app qa/app.yaml --parallel 6 --reports-dir qa/_pass2-control/reports --synthesis-dir qa/_pass2-control/synthesis`. Elapsed: **303.3s.** Cost: ~$0.70.

### 5.1 Delta vs Forge

| Metric | Forge (untuned) | careerops (tuned, 6 personas) | Delta |
|---|:---:|:---:|---|
| Total friction events | 8 | 71 | careerops 9× more (mostly more personas + complexity) |
| Events per persona | 4.0 | 11.8 | careerops 3× denser |
| Actions clicked per persona | 0–1 | 4–6 | **The instrumentation-coverage delta** — careerops's `data-action` hooks make actions probeable; Forge's vanilla HTML doesn't expose action attrs. |
| `instrumentation_gap` events | 0 (0%) | 0 (0%) | **No gap-dump on either.** |
| High-confidence events | 5 (62%) | 45 (63%) | **Nearly identical.** |
| Medium-confidence events | 2 (25%) | 21 (30%) | Close. |
| Low-confidence events | 1 (12%) | 5 (7%) | careerops slightly lower (expected — tuned config). |
| Synthesizer re-prompt fired | NO | NO | Patches stayed dormant on both. |
| Confidence demoter fired | NO | NO | Same. |

### 5.2 The instrumentation delta — what this means for unfamiliar codebases

The most visible delta is **actions clicked per persona** (0-1 on Forge vs 4-6 on careerops). Forge's vanilla HTML has standard CSS class selectors (`.c-card`, `.cat-pill`, `button#publish-btn`) but lacks the `data-action="*"` attribute hooks the framework prefers. The framework still works — it just probes by CSS class instead — but persona traversal is shallower because fewer probes match.

Implication for unfamiliar codebases: CohortQA will produce **less interaction-driven friction** on apps without instrumentation, leaning more on observational friction (load times, body length, visible elements). For Forge, that's still surfaced 6 patterns of real friction — so the lack of action hooks isn't fatal. But the framework benefits substantially from `data-action` instrumentation, and ports to apps without it produce shallower runs.

### 5.3 The "no FP after tuning" claim, tested

The README implies ~0% FP rate on careerops after the 43%→0% historical fix. Empirically: careerops control had 5 low-confidence events out of 71 (7%) — not literally zero, but very low. And **0 instrumentation_gap events** on the home turf. The fix held.

On Forge: 1 low-confidence event out of 8 (12%) and 0 instrumentation_gap. The framework's escape hatch (mark uncertain findings as instrumentation_gap) did NOT fire on either app — meaning the analyzer was confident in every finding it produced. The wrong-type labeling (§3.4) is a separate failure mode the confidence system doesn't catch.

---

## 6. The honest FOLD-BACK case

A reviewer pushing for FOLD-BACK would argue:

1. **CohortQA found 3 unique findings; plain Claude Code found 7 unique findings.** Net-net, plain CC produced more signal in less time with zero setup. The "CohortQA finds things plain CC can't" claim is true but small.
2. **The cross-persona value is real but narrow.** Two of the six patterns are genuinely cross-persona; the other four are findings either persona alone would have surfaced. So CohortQA's distinctive value is ~2 patterns per Forge run.
3. **Setup cost is non-trivial.** 25 min wall-clock on first run is 1.5× a manual exploration. For a team that ships rarely or has a real designer, this is a poor ROI.
4. **The `data_density`-for-sparse-content labeling error (§3.4) is in the analyzer prompt, not core architecture** — but it's a quality issue that would show up in any published version and erode trust.
5. **Plain CC can be re-run cheaply too.** The "amortizes across releases" argument applies to plain CC also — re-running a one-paragraph prompt against a browser is free.

The FOLD-BACK case is articulable. I'm not calling it because the cross-persona empirical evidence DID come through and because the framework didn't degrade on an unfamiliar codebase (no gap-dump, no synth-patches firing, comparable confidence distribution). But the case for FOLD-BACK is closer than the prior rereview's "narrow survival" framing suggested.

---

## 7. Recommended next moves

1. **Do the 1-2 day rework from the prior rereview** before publishing — but with two additions:
   - **Fix the `data_density`-vs-`empty_state` labeling confusion in the analyzer prompt** (`analyzer.py:150-212`). Sparse content (low body_text_length, few elements) should always file as `empty_state`, never `data_density`. A 5-line clarification in the friction-taxonomy block.
   - **Keep the confidence-demoter and re-prompt fallback for now** — they didn't fire on Forge OR careerops, so they're dormant overhead, not active patches. Re-evaluate after the analyzer prompt is hardened.
2. **Publish with the right-sized framing:** "CohortQA — a cross-persona UX-pattern detector for solo and small-team AI-app builders. Runs as a release-over-release regression harness alongside manual QA / plain Claude Code passes." NOT "AI QA platform."
3. **In the README, add an explicit "What plain Claude Code finds that CohortQA doesn't, and vice versa" section** based on this audit. Trust comes from honest scope, not overclaim.
4. **Skip the `nav_error` and `data_inconsistency` taxonomy additions for now.** Forge surfaced neither category convincingly in this run; add them only when a real second app needs them. YAGNI applied to the framework.

---

## Files touched

- `forge-qa/app.yaml` — 4 routes, 16 actions, 9 friction signals describing Forge to CohortQA
- `forge-qa/personas/tool-submitter.yaml` — engineer publishing a tool
- `forge-qa/personas/tool-browser.yaml` — non-technical operator browsing
- `forge-qa/runs/`, `forge-qa/reports/`, `forge-qa/synthesis/` — generated artifacts (gitignored under the cohortqa default `runs/` patterns; checked-in only the polish-spec markdown for evidence)
- `qa/_pass2-control/reports/`, `qa/_pass2-control/synthesis/` — careerops control artifacts (generated; do not edit)
- This audit doc.

**No edits to `cohortqa/core/`, `cohortqa/schemas/`, or any production code.** The forge-qa/ config is a new top-level dir; the careerops control output is under `qa/_pass2-control/` to isolate from the existing `qa/reports/`.

---

## Operational notes

- **Forge was already running** when this audit started (PID 97005, `api/server.py` on `:8090`, started before this session). Did NOT stop it — the user has it up for their own work. If you want it stopped: `kill 97005`.
- **Live Hetzner box (178.156.244.15) was NOT touched.** All Phase-2 testing was against `http://localhost:8090`.
- **No real marketplace submissions were created in Forge's Postgres.** The `click_publish` action was protected via `side_effects: writes:` and never clicked.
- **CohortQA dependencies installed during this run:** `playwright==1.60.0`, `pyee==13.0.1`, `greenlet==3.2.5`, plus Chromium headless shell v148. These are local-user installs (`~/Library/Python/3.9/...` and `~/Library/Caches/ms-playwright/...`) — not committed.

---

## Spend accounting

| Phase | Approx cost |
|---|---:|
| Phase 0 (find Forge, verify running) | $0.00 (Forge was already up; no API spend) |
| Phase 1 (config build) | ~$0.20 (Read + Write + grep tokens) |
| Phase 2 (CohortQA on Forge, 2 personas + synth) | ~$0.30 |
| Phase 3 (plain Claude Code agent, 51K tokens) | ~$0.30 |
| Phase 4 (CohortQA on careerops control, 6 personas + synth) | ~$0.70 |
| Phase 5 (this audit + commit) | ~$0.20 |
| **Total** | **~$1.70 of $4.00 budget** |

Branch: `cohortqa/forge-pass2`. No push, no merge.
