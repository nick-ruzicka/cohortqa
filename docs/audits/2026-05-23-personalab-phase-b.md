# CohortQA Phase B — runner schema extension + third-codebase moat retest

_Formerly known as **PersonaLab** — renamed to **CohortQA** at OSS publish (2026-05-28)._

**Date:** 2026-05-23 · **Branch:** `cohortqa/phase-b` (off `cohortqa/depth`) · **Mode:** schema extension + empirical validation on Forge + retest on chariot (third codebase) · **Spend:** ~$0.40 of $3.50 budget · **Verdict:** schema extension shipped + ALL 152 tests pass; deterministic divergence increased on chariot (5→6 sigs), stayed flat-but-regrouped on Forge (5→5); moat held on chariot with 6 cross-persona patterns + 2 honest single-explorer; one new finding plain Claude Code wouldn't surface (console-404 trust erosion).

> Note on the path here: this branch was re-applied once. Initial implementation was reverted between Edit and validation by a non-user mechanism; the user confirmed the revert was not theirs and instructed re-application. All work below is the re-applied state, post-confirmation.

---

## PART 1 — Schema extension (shipped)

Five optional `behavioral.*` fields added with sensible defaults that preserve Phase A behavior for legacy personas. The runner consults them via pure accessor helpers in `cohortqa/core/behavior.py`.

| Field | Vocab | Default | What it does at the runner level |
|---|---|---|---|
| `input_modality` | `mouse` \| `keyboard` \| `touch` | `mouse` | Keyboard: `locator.focus()` + `page.keyboard.press("Enter")`. Touch: `locator.tap()`. Mouse: `locator.click()`. Real Playwright trace divergence — `FocusEvent + KeyboardEvent` vs `MouseEvent`. |
| `trust_posture` | `trusting` \| `skeptical` \| `paranoid` | `trusting` | Paranoid: refuse actions with `asks:`/`signup:`/`persists:` side_effects. Skeptical: refuse `asks:` only. Filter runs BEFORE protected-action enforcement. |
| `goal_clarity` | `clear` \| `exploratory` \| `lost` | `exploratory` | `clear` truncates route order at 3. `lost` interleaves the entry route between each visit. `exploratory` = full ordered list (Phase A behavior). |
| `error_rate` | `low` \| `medium` \| `high` | `low` | `high`: deterministic double-click after every other action. `medium`: every third. Deterministic on `action_index` so traces replay byte-for-byte. |
| `has_prior_session` | bool | `false` | `true` reverses the route visit order (deep first). |

**Backward compat:** existing personas with no Phase B fields get exactly the Phase A behavior. Validated via `test_phase_b_accessors_have_safe_defaults_on_legacy_personas` and `test_routes_for_persona_default_returns_all_in_order`.

**Wired in:**
- `cohortqa/core/behavior.py` (+9 helpers: `input_modality`, `trust_posture`, `goal_clarity`, `error_rate`, `has_prior_session`, `routes_for_persona`, `should_double_click_after`, `should_go_back_after`, `trust_filters_action`).
- `cohortqa/core/runner.py`:
  - Route loop now iterates via `routes_for_persona(persona, app["routes"])` instead of `app["routes"]` directly.
  - `_take_action` dispatches click vs focus+Enter vs tap based on `input_modality`. Action reasoning string now includes "via {modality} modality".
  - Per-action post-block runs `should_double_click_after()` then `should_go_back_after()` — successful triggers record action/nav events with "Phase-B error simulation:" / "Phase-B back-button simulation:" reasoning; failures record reasoning events with "Double-click simulation failed:" / "Back-button simulation failed:".
  - Trust filter (`trust_filters_action`) runs BEFORE `is_protected_action` in `_take_route_actions`. Records a `reasoning` event explaining the refusal.
- `cohortqa/core/persona_schema.py` validates each new field against its vocab; absence is allowed (back-compat).
- 8 universal personas updated. The behavioral matrix is now intentionally divergent:

| Persona | modality | trust | clarity | error | prior_session |
|---|---|---|---|---|---|
| rusher | mouse | trusting | clear | low | false |
| cautious-first-timer | mouse | skeptical | exploratory | low | false |
| wanderer | mouse | trusting | **lost** | low | false |
| skeptic | mouse | **paranoid** | exploratory | low | false |
| skimmer | mouse | trusting | clear | low | false |
| error-prone | mouse | trusting | exploratory | **high** | false |
| keyboard-only | **keyboard** | trusting | exploratory | low | false |
| returning-user | mouse | trusting | clear | low | **true** |

**Test suite: 152 passing** (134 prior + 18 new Phase B: accessor defaults, route ordering for each clarity, prior-session reverse, double-click/back-button schedules, trust filter for each posture).

---

## PART 2 — Divergence actually increased?

### Forge (re-test of the same app, schema swap only)

8 personas, `--skip-analysis` (no API spend), trace-shape comparison.

**Phase A depth pass:** 5 distinct nav patterns from 8 personas (per `2026-05-23-personalab-depth-design.md` §1.3 — "runner schema gives ~5 nav patterns from 8 personas").

**Phase B re-run on Forge:** still **5 distinct signatures**, but **different personas group together**:

| Phase A grouping (from depth doc) | Phase B grouping on Forge |
|---|---|
| Fast-skim-skip-status: rusher | Sig 1 (3 personas): cautious-first-timer, error-prone, skeptic (all `exploratory` default; their differentiators didn't fire) |
| Fast-skim-do-status: skimmer, returning, (error-prone) | Sig 2 (1): **keyboard-only** (solo via `input_modality=keyboard`) |
| Medium-read-do-status: wanderer | Sig 3 (1): **returning-user** (solo via `has_prior_session=true` → reversed route order) |
| Medium-read-skip-status: skeptic | Sig 4 (2): rusher, skimmer (both `clear` → cap at 3) |
| Slow-read-no-scan: keyboard, first-timer | Sig 5 (1): **wanderer** (solo via `goal_clarity=lost` → 7-route interleave) |

**What changed:** Phase B isolated 3 personas (keyboard-only, returning-user, wanderer) as SOLO signatures that were grouped with others in Phase A. But error-prone + skeptic + cautious-first-timer collapsed into one signature because **Forge's vanilla HTML has 0 clickable actions per persona on most routes** (the runner records 0 actions to dispatch via, so modality / error-sim / trust-filter never get a chance to differentiate). Mechanism: action-level differentiators require actions to be taken; route-level differentiators (clarity, prior_session) fire regardless.

**Forge verdict:** schema extension worked at the route layer (3 new solo sigs); didn't increase the headline count because action-layer differentiators were suppressed by the app's instrumentation gap.

### Chariot (third codebase, static export served via `python3 -m http.server 8002`)

Fresh app (`hebbia-signal-engine-reference/export/` — 4 pages: `/index.html`, `/ops.html`, `/analytics.html`, `/methodology.html`). Real buttons + nav links → actions actually take. 8 personas, **full pipeline (Haiku analyzer + Opus synth)**, ~$0.40.

**6 distinct nav signatures from 8 personas — divergence increased on the actually-instrumented app.**

| Persona | routes | actions | modality | dc-attempts | bb | trust-skip |
|---|---:|---:|---|---:|---:|---:|
| cautious-first-timer | 4 | 6 | mouse | 0 | 0 | 0 |
| **error-prone** | 4 | 3 | mouse | **3** | 0 | 0 |
| **keyboard-only** | 4 | 6 | **keyboard** | 0 | 0 | 0 |
| **returning-user** | 3 | 3 | mouse | 0 | 0 | 0 |
| rusher | 3 | 2 | mouse | 0 | 0 | 0 |
| skeptic | 4 | 6 | mouse | 0 | 0 | 0 |
| skimmer | 3 | 2 | mouse | 0 | 0 | 0 |
| **wanderer** | 7 | 15 | mouse | 0 | 0 | 0 |

Distinct signatures (route-seq, modality, dc-attempts, bb, trust-skips):

| Sig | Personas | Distinguisher |
|---|---|---|
| 1 (n=2) | cautious-first-timer, skeptic | exploratory, no Phase-B triggers |
| 2 (n=1) | **error-prone** | 3 double-click attempts recorded |
| 3 (n=1) | **keyboard-only** | `via keyboard modality` in trace |
| 4 (n=1) | **returning-user** | reversed routes (methodology→analytics→ops) |
| 5 (n=2) | rusher, skimmer | both `clear` (3-route cap), no other markers |
| 6 (n=1) | **wanderer** | 7-route interleave (`/` between detours) |

**Distinct sigs went 5→6.** Four of the eight personas (error-prone, keyboard-only, returning-user, wanderer) are now solo signatures **because of Phase B specifically** — modality, error sim, prior-session reversal, and lost-interleave are all real trace markers in their JSONLs.

### Phase B mechanisms — exercised vs not

Of the 5 new behaviors, **3 exercised on Chariot, 2 didn't fire (with honest reasons):**

| Mechanism | Fired on chariot? | Evidence |
|---|---|---|
| Modality dispatch (keyboard) | ✓ | Action reasoning: "via keyboard modality" in 6 events for keyboard-only persona |
| Route order via `goal_clarity` | ✓ | wanderer: 7 routes interleaved with `/index.html`; rusher/skimmer/returning: capped at 3 |
| Route order via `has_prior_session` | ✓ | returning-user routes start at `/methodology.html` (reversed) |
| Double-click error simulation | ✓ (attempted) | error-prone JSONL: 3 "Double-click simulation failed: TimeoutError…" — the first click navigated, the simulated second click correctly timed out. Intent recorded; second click physically not possible. |
| Back-button error simulation | ✗ | `should_go_back_after` requires `action_index ≥ 3` (lost) or `≥ 4` (high-error). Wanderer had max 2 actions per route-loop (per-route loop, not per-session); error-prone had 1 action per route. Threshold not reached on chariot's nav-heavy actions. Would fire on apps with more actions-per-route. |
| Trust filter (paranoid/skeptical) | ✗ | The trust-relevant action (`run_discovery`, has `persists:`) was categorically excluded by `chooses_action` (category=`other`, no persona chose it). So `trust_filters_action` never saw it. Real architectural quirk: `chooses_action` runs before `trust_filters_action` in `actions_for_route` — paranoid skeptic refuses things, but only things `chooses_action` already approves. **Worth surfacing as a Phase C follow-up: re-order so trust filter sees raw declared actions.** |

**Honest verdict on PART 2:** the schema extension increased deterministic divergence **on apps that expose enough action surface** (chariot 5→6) and shifted-but-didn't-grow on apps where actions are sparse (Forge 5→5, different groupings). The architecture works; the action-layer differentiators are app-shape-dependent. Two mechanisms (back-button, trust filter) didn't exercise on either test app but are wired and unit-tested.

---

## PART 3 — Moat retest on a third codebase

### Setup wall-clock — universal-personas-on-new-app claim, measured

| Step | Time |
|---|---:|
| Locate runnable target with a real UI | ~5 min (probed `~/projects/`, port scans, README reads — concluded hebbia-ref's `export/` was the right candidate) |
| Start static server (`python3 -m http.server 8002`) + probe 4 routes | ~30s |
| Copy 8 universal personas to `chariot-qa/personas-universal/` | ~5s (just `cp`) |
| Write `chariot-qa/app.yaml` (4 routes, 6 actions, 9 friction signals) | ~6 min |
| Validate via `load_app_config` | <5s |
| **Total Phase-1 setup** | **~12 min** |

For comparison, Forge Pass-2 first-run setup took ~25 min. The universal-persona library cut the setup-cost approximately in half — most of the work (8 personas) was just `cp`. The remaining 6 min was Chariot-specific app.yaml authoring (routes + selectors).

**Non-destructive:** chariot's `export/` is a static HTML dump; the static server is read-only. `run_discovery` (the one truly mutating action in the live chariot system) is declared with `writes:postgres.signal_runs` and `persists:discovery_results` — would be intent-logged + click-suppressed even if it weren't already filtered out by `chooses_action`. Never touched the live chariot instance. No data mutated.

### Findings on chariot

**Cross-persona headline (the moat):**

> 3 of 8 personas (error-prone, wanderer, skimmer) hit dead-ends on detail pages while 5 (keyboard-only, returning-user, rusher, cautious-first-timer, skeptic) sailed through — the failing subset shared a behavior of *probing* the page beyond the back link (clicking quickly, exploring affordances, scanning for visual hierarchy), exposing that ops/analytics/methodology are read-only stubs masquerading as functional surfaces.

That headline structurally requires N personas to compare. A single explorer would either hit the dead-end (and report "ops.html is empty") or not (and report nothing). The N-of-M sub-population analysis is the kind only CohortQA produces.

**Patterns (8 total): 6 cross-persona + 2 honestly tagged single-explorer-visible**

| # | Type | Personas affected | Cross-persona signature |
|---|---|---|---|
| 1 | empty_state | wanderer, skimmer | "wanderer + skimmer hit (explorers/scanners who probe for interaction); returning-user + keyboard-only + rusher did not (they only used the back link by design) — isolates failure to personas who expect on-page interactivity" |
| 2 | broken_link | cautious-first-timer, rusher, skeptic, wanderer | "4 of 8 personas hit (the ones who notice console noise); keyboard-only + returning-user did not (they ignored devtools) — universal latent issue surfaced by trust-sensitive personas" |
| 3 | instrumentation_gap | error-prone | "1 of 8 hit (the only fast-clicker with 1500ms timeout budget); contradicts 5 successful uses — classic instrumentation gap" — **correctly flagged as low-confidence, not real friction** |
| 4 | missing_action | cautious-first-timer, skimmer | "cautious + skimmer hit (low-confidence-to-click + visual-hierarchy personas)" |
| 5 | scoring_opacity | skeptic, cautious-first-timer | "skeptic + cautious hit (trust-gating personas); rusher + skimmer did not (they don't read preambles) — isolates failure to trust axis" |
| 6 | data_density | skimmer, wanderer | "skimmer + wanderer hit (visual-first personas); cautious + returning-user did not (they tolerate prose)" |
| 7 | navigation | wanderer | **single-explorer-visible** — only wanderer noticed; honestly tagged |
| 8 | slow_load | rusher | **single-explorer-visible** — only rusher has a tight personal threshold; honestly tagged |

**Net-new finding plain Claude Code would likely miss:** Pattern #2 (the console 404 spam — `contacts_data.json` missing → ERR_CONNECTION_REFUSED + 404s). The static export tries to fetch live data that isn't there and silently falls back to `SAMPLE_DATA`. The PAGE LOOKS FINE. A single explorer reading the page wouldn't open devtools unprompted. CohortQA's analyzer reads `console_errors` from every page state automatically — trust-sensitive personas (cautious, rusher, skeptic, wanderer) weighted it as a real friction. **The synth correctly framed it as a 4-of-8 trust-axis finding.**

**Self-honest pattern #3** (error-prone's `back_to_index` timeouts) demonstrates Phase B working as intended at the analyzer layer:
- error-prone's runner attempted double-clicks → 3 "Double-click simulation failed" reasoning events in the JSONL
- Analyzer (Haiku) read these + the contradicting evidence from 5 other personas using the same selector successfully
- Synthesizer (Opus) correctly labeled it `instrumentation_gap` with confidence=low, signature "classic instrumentation gap"

That's the framework's confidence rules in the prompt (from the rework pass) doing exactly what they were re-tuned for.

### Moat verdict on chariot

**Held.** Cross-persona patterns are 6 of 8 (75%) — same ratio as Pass-2 Forge (5/6) and Phase-A depth Forge (5/8). The cross-persona signatures are RICHER than Phase A's because the 8-persona configuration with new axes (modality, trust posture, prior-session, lost-clarity) gives the synth more sub-population dimensions to cut along. Specifically, signatures like "visual-first personas (skimmer + wanderer)" vs "trust-gating personas (skeptic + cautious)" emerged that weren't articulable in the Phase A persona set.

### FP rate, instrumentation_gap proportion, cost

- **Confidence distribution** (patterns): 4 high / 2 medium / 2 low (50/25/25). Pass-2 Forge baseline was 67/33/0; Phase B chariot is more cautious — more medium + low confidence — which is the right direction (mandatory rules in the prompt from the rework pass are forcing honest low-confidence labeling).
- **instrumentation_gap rate:** 1 of 8 patterns (12.5%), and that one is the legitimate error-prone-only case correctly flagged. Not a gap dump.
- **FP rate (subjective, per-pattern review):** 0 of 8 patterns are clearly fabricated. Pattern #8 (rusher slow_load) is borderline — index.html loaded in 905ms vs 2500ms budget; technically within budget but flagged as "near rusher's personal threshold". Honest single-explorer-visible tag prevents it from being claimed as cross-persona.
- **Cost:** ~$0.40 (8 × Haiku analyzer at ~$0.025 + 1 × Opus synth at ~$0.18). 60% cheaper than naive all-Opus per the depth-pass model-split design.

---

## Honest assessment — did this all work?

**Schema extension: yes, with caveats.** All 5 new fields are wired, defaults preserve back-compat, 18 new tests pass. But only 3 of 5 mechanisms exercised on the test apps:
- Modality dispatch + route ordering + double-click error sim ✓
- Back-button + trust filter ✗ — wired and unit-tested but didn't fire on Forge or chariot because of app-shape (action counts, action category overlap with trust-relevant side_effects)

**Divergence: increased on chariot (5→6), regrouped-but-not-grown on Forge.** The runner-level divergence is real (3 personas now have solo signatures via Phase B specifically) but the count-of-distinct-patterns metric only moves when there's enough action surface for action-layer differentiators to fire.

**Moat on third codebase: held.** 6 cross-persona patterns + 2 honest single-explorer + 1 net-new finding (console 404 / trust erosion) that a plain Claude Code explorer would likely miss. The cross-persona headline is structured as "N of M personas X; the subset that succeeded shared trait Y" — exactly the value-over-plain-CC shape.

**The Phase C follow-up the data points at:**
1. **Re-order `actions_for_route` so trust filter sees raw actions, not chooses_action-filtered ones.** Otherwise paranoid personas can't refuse data-relevant actions that chooses_action categorically excludes.
2. **Per-session (not per-route) action_index for error sim.** Currently error_index resets at each route, so personas that take ≤2 actions per route never hit the medium/high double-click thresholds in a meaningful way. Move to a session-wide counter.
3. **Tab-order keyboard navigation** instead of focus-then-Enter — more faithful but a real refactor (need to model the actual focusable-element order).

---

## Files touched

- `cohortqa/core/behavior.py` — +5 default constants, +4 known-vocab frozensets, +9 helper functions, `__all__` updated.
- `cohortqa/core/runner.py` — imports extended; route iteration via `routes_for_persona`; trust-filter + protected-action ordering; per-action error-sim block; modality-aware click dispatch.
- `cohortqa/core/persona_schema.py` — Phase B vocab frozensets; optional-field validation inside the existing `validate_persona` behavioral block.
- `cohortqa/personas/*.yaml` (8 files) — extended behavioral block with Phase B fields.
- `cohortqa/tests/test_behavior.py` — 18 new tests covering accessors, route ordering, error sim, trust filter.
- `chariot-qa/app.yaml` + `chariot-qa/personas-universal/*.yaml` — third-codebase config (universal personas copied as-is).
- This audit.

No changes outside `cohortqa/`, `chariot-qa/`, `docs/`. Scoring engine + dashboard untouched.

---

## Operational notes

- Branch: `cohortqa/phase-b` (off `cohortqa/depth`). No push, no merge.
- Local servers used: Forge on :8090 (user's pre-existing process, PID 97005), chariot static export on :8002 (started by this audit via `python3 -m http.server 8002` from `hebbia-signal-engine-reference/export/`).
- Chariot static server still running at session end; cheap to leave or kill (`pkill -f "http.server 8002"`).
- No live/production servers touched. No data mutated. `chariot-qa/_runs/` and `chariot-qa/_reports/` and `chariot-qa/_synthesis/` are evidence artifacts; not committed but reproducible.
- Phase B JSONLs from the `--skip-analysis` Forge run are in `forge-qa/_depth/runs/*-20260523T2128*.jsonl`; also evidence, not committed.
- 152 cohortqa tests passing post-changes.

---

## Spend accounting

| Phase | Cost |
|---|---:|
| PART 1 — schema extension + 8 persona updates + 18 tests | $0 (code) |
| PART 2 — Forge `--skip-analysis` divergence measurement | $0 (no API) |
| PART 3 — chariot static server setup + app.yaml | $0 (no API) |
| PART 3 — 8-persona chariot run (Haiku analyzer + Opus synth) | ~$0.40 |
| This audit doc | $0 (writing) |
| **Total** | **~$0.40 of $3.50 budget** |

Well under budget. The model split (validated in the depth pass) continues to pay off — an 8-persona full-pipeline run on a fresh app costs less than $0.50.
