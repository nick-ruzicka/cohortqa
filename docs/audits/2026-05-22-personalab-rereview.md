# PersonaLab Architectural Re-Review — Kill Funnel

**Date:** 2026-05-22 · **Mode:** read-only, conceptual · **Branch:** `docs/audit-gtm-fde-onboarding-extension` · **Posture:** adversarial. Try hardest to kill at each level. No flattery.

## Funnel verdict (one line)

**Survives L1 narrowly. Survives L2 with downgraded framing ("useful niche OSS tool" not "product"). Fails L3 cleanly (moderate rework before publish).** Honest recommendation at the end: **rework-then-ship-standalone, gated on a Forge Pass-2 that doesn't disappoint** — and if Forge disappoints, **fold back into career-ops as a fixture and don't publish.**

---

## LEVEL 1 — Should persona-driven QA exist?

### The strongest case AGAINST

1. **A 30-minute human walkthrough catches the same affordance bugs.** Real PMs / designers / engineers reading the UI find "this button is missing," "this label is confusing," "this is slow" in minutes. The bugs PersonaLab catches are bugs a competent half-day of QA catches. Adding an LLM-and-Playwright pipeline to do what an attentive human does in coffee-break time is *process burden disguised as scale*.
2. **Playwright tests in CI already catch the deterministic regressions.** A real E2E suite catches 404s, broken click paths, missing buttons, and timeouts before they merge. The PersonaLab runner is itself Playwright + heuristics — so the marginal "ML" content is just the analyzer's labeling, not the discovery.
3. **The personas are Claude's model of what a persona thinks.** Even though the runner is deterministic (`behavior.py` — pure rules, no LLM, `actions_for_route` at `runner.py:345`), the friction labeling in `analyzer.py:450` is Claude reading observations *through a persona-lens system prompt*. That step is Claude predicting persona reactions. If the LLM has a wrong prior on what a "Senior GTM Engineer" cares about, the labels are wrong; you cannot tell if a real Senior GTM Engineer would actually feel that friction.
4. **The scope is shallow.** The taxonomy is explicit (`qa/app.yaml:47-85`, `persona_schema.py:39-49`): navigation, scoring_opacity, archetype_confusion, data_density, missing_action, broken_link, slow_load, empty_state, instrumentation_gap. **Nine signals, all affordance/UX-flavored.** It does not catch server errors, data integrity, cross-component consistency. The user's memory-flag stands: Gstack caught those bugs; PersonaLab didn't. The bugs that *ship and break things in prod* are typically the bugs PersonaLab doesn't catch. The bugs PersonaLab catches are the bugs that hurt retention / activation — important, but not the highest-severity class.
5. **Process burden vs signal yield.** Setup is 4–8 hours per app (write `app.yaml` with N routes + expected_load_time_ms + friction_signals, write 6 personas, write scenarios). $1/run. For most teams shipping at normal cadence, that setup cost dwarfs the savings vs a 30-min manual QA pass.

### The strongest case FOR (what survives the attack)

1. **The data is real; only the labeling is LLM.** Playwright captures honest observations: console errors, page text, selector matches, load times, navigation errors. Claude doesn't *drive* personas (behavior.py is deterministic, zero LLM); Claude only reads logs and labels friction. This is **not "AI simulates a user end-to-end"**; this is "AI helps you read your real telemetry through a user's eyes." Much weaker self-reference than I feared going in.
2. **Cross-persona pattern detection over telemetry is non-trivial.** A single Claude call across 6 friction reports surfaces "5/6 personas hit the same dead-end at `/signals`" in seconds. Doing that by hand across 6 long markdown reports takes 30+ minutes per release. **This is the irreducible value: cross-persona pattern synthesis over deterministic telemetry.**
3. **Personas encode users you don't have access to.** A skeptical CFO, a new-to-platform engineer, a power-user — you can't always recruit these. Encoding them as YAML lets you re-run the same exam every release. Even if Claude's persona-model is imperfect, an imperfect repeatable proxy beats no proxy.
4. **The 43%→0% FP-rate history shows honest engineering, not theatre.** The fix (selector_probe abstraction `runner.py:304-308` + hydration wait `runner.py:275` + new `instrumentation_gap` friction type `persona_schema.py:48`) is structural, not a band-aid. Most "AI QA" products would have sold the hallucinated friction and called it a feature. PersonaLab engaged with the failure mode honestly.

### L1 verdict

**Narrow survival.** The case against is real — a thoughtful human-plus-Playwright covers ~80% of what PersonaLab catches. The case for has an irreducible kernel:

> **Cross-persona pattern synthesis over real Playwright telemetry, with personas providing a perspective filter that surfaces issues a generic E2E test doesn't flag as failing but a specific user would experience as friction.**

That is real but narrow. It is *not* "AI QA replaces designers." It is "a per-release UX-friction digest for teams without a dedicated UX-research function." If you accept that framing, L1 survives.

If the user wants me to kill it at L1 — **I won't**. The irreducible-value claim is defensible. But I would refuse the broader framing ("AI persona QA changes how we ship") and accept only the narrower one.

---

## LEVEL 2 — Does the scope matter?

### Friction-class size

UX/affordance friction is real and ships often. SaaS adoption literature treats "confusing UI" as a top churn driver. But: **the audience that needs this most has already solved it with humans.** Teams with a designer/PM/UX-researcher catch these issues in design review and beta testing. Teams without that function are typically solo builders or 2-3-person teams — a small audience.

### Who's the user?

- **Primary:** A solo or 2-3-person team builder shipping an AI app, no designer, no UX researcher, no QA function. PersonaLab gives them a "what would my users feel?" digest before each release.
- **Secondary:** A larger team that wants a regression check on user experience between releases — a "did we get worse?" signal.
- **Anti-user:** Anyone shipping infrastructure / data products. PersonaLab's UX-friction taxonomy is the wrong tool for backend correctness.

### Effort-in vs signal-out

**First-run cost:** 4–8 hours for app.yaml + 4-6 personas + scenarios. **Per-release cost:** ~5 min CLI + ~$1 compute. **Signal:** ranked polish spec markdown.

Verdict: **amortizes well for teams that ship frequently (≥monthly)**. For teams shipping ≤quarterly, the setup cost is steeper than the savings vs manual QA.

### Niche or product?

**Niche.** Honest sizing:
- AI-app builders who care about UX: thousands.
- That subset that lacks a designer and ships frequently and adopts dev tools: hundreds-to-low-thousands.
- That subset that adopts an OSS Python framework with Playwright + Claude API setup: low hundreds to low thousands.

This is not a VC-scale product. It is "a useful niche OSS tool with a few hundred users at maturity, possibly a small paid SaaS later if the audience self-organizes."

### L2 verdict

**Survives with downgraded framing.** This is a **useful niche OSS tool**, not a product. Anyone framing it as a startup / a market is overreaching. Anyone framing it as "a useful thing I built that I'm sharing" is right-sizing.

If the user wants me to kill it at L2 because "niche" is fatal — I won't. Plenty of useful OSS lives at this size (sqlfluff, mitmproxy, locust, etc.). The scope is enough to *exist*. It is not enough to *be a product*. Right-size accordingly.

---

## LEVEL 3 — Is it built right?

### What's clean (backbone)

- **`runner.py` (766 LOC, no LLM)** — Playwright session loop, JSONL event schema. Stable, deterministic, well-tested. The contract every downstream consumer (analyzer, replayer, synthesizer) reads. Architecturally first-class.
- **`analyzer.py` (521 LOC, 1 Claude call)** — friction extraction per session. Prompt is structured (taxonomy + persona + JSONL), output is structured (FrictionReport with timestamps anchoring each event to telemetry). Load-bearing.
- **`orchestrator.py` (369 LOC)** — async fan-out + sequence. Boring glue, the right amount of complexity.
- **`persona_schema.py` (392 LOC)** — hand-rolled validators, no jsonschema dep. Boring but solid. Source of truth for `KNOWN_FRICTION_TYPES`.
- **`behavior.py` (161 LOC, no LLM)** — pure persona-decision rules (click speed, action selection). Testable, deterministic. Well-isolated.

### What's accreted (patches over time)

- **`synthesizer.py` (574 LOC, largest module, most amendments).** The core synthesis is sound (one Claude call across all per-persona reports, output structured PolishSpec). But two post-hoc patches:
  - **Re-prompt fallback (`synthesizer.py:390-408`, commit `ea1c031`):** if `patterns == []` AND the prose summary appears to mention groups, make a second Claude call asking to structure the prose. The trigger is a **regex on English words (`"cluster|pattern|group|category|theme"`, lines 520-530)** — fragile, English-only, model-version-sensitive.
  - **Confidence demoter (`synthesizer.py:477-530`, commit `52c0b8f`):** mechanical rule — if a pattern's signal_type is `instrumentation_gap` or a majority of contributing per-persona findings have `confidence=low`, demote the pattern. This is defensive (guards against "6/6 personas reported it" when they reported the same root cause) but smells like **work the prompt should have done**. If the model needs to be guard-railed against its own output after the call, the prompt isn't tight enough.
- **`_credit_check.py` (53 LOC)** — operational patch added after the analyzer/synthesizer started hitting budget limits. Necessary but not core logic.

### What's optional (could be cut without breaking the pipeline)

- **`scenario_runner.py`** — DOM-inject + diff. Clean abstraction, but not in the main orchestrator loop; called separately. Cool feature, not load-bearing.
- **`replayer.py`** — re-run a recorded session against current code, check for drift. Same status: clean, optional.

### Is the 43%→0% FP fix robust?

**70% structural, 30% tuned.**
- Structural (robust): selector_probe (`runner.py:304-308`), hydration wait (`runner.py:275`), `instrumentation_gap` taxonomy slot (`persona_schema.py:48`). These will hold up on any new app — they fixed real telemetry-shape problems, not CareerOps-specific patterns.
- Tuned (fragile): the synthesizer re-prompt regex (English word match) and the confidence demoter (defensive guardrail). These will likely *also* hold up on a different app — but for a different reason than they fired on CareerOps. They are patches around model-output-shape failures, not telemetry-shape failures. If the model version changes (Sonnet 4.6 → Opus 4.7 → next), they may stop triggering correctly. **A new app porting PersonaLab today probably won't FP. A new app porting PersonaLab in 18 months with a newer model might.**

### Rebuild-it-today list

If I rebuilt the architecture today knowing what I know now:

1. **Synthesizer should use Anthropic structured output (strict JSON schema) at the API call** — kill the re-prompt regex entirely. The fact that the prompt requests "patterns" and the model returns prose-summary-with-groups is a model-following failure that strict output forcing solves at the API level, not via regex post-processing.
2. **Confidence demoter rules go INTO the prompt**, not after. Tell Claude: "if a finding's underlying confidence is low, the pattern confidence MUST be low" as a hard rule in the system prompt. Eliminate the post-hoc guardrail.
3. **Promote `nav_error` to a first-class friction type.** The runner already captures it (`runner.py:264-265`), but it has no taxonomy slot — analyzer fudges it into missing_action or empty_state. The user's memory-flag about server errors not being caught is *partially fixable* with this change.
4. **Add a `data_inconsistency` friction type for cross-component consistency.** Catch "company card shows X but detail page shows Y." Requires the runner to record entity-attribute observations across routes (small extension to JSONL schema) and the analyzer to do an inter-route comparison pass. This addresses the *real* memory-flag scope limitation (cross-component consistency).
5. **Default friction taxonomy ships with the framework**, not redeclared per app. Today every app.yaml repeats the 9 types. The framework should provide them as a default; apps add domain-specific extras.
6. **scenario_runner + replayer move to `personalab.extras/` or a separate package.** Useful but optional; cleaner core if they're out of the main module.
7. **Behavior rules become persona-attributes-as-inputs to a tiny DSL**, not hardcoded click_speed/detail_dwell_ms switches. Easier to extend without code changes.

### L3 verdict

**Needs moderate rework.** The bones are good; two specific joints (synthesizer prompt-hardening, taxonomy expansion for server-error + data-inconsistency) need a 1-2 day pass before public ship. The framework as-is would publish today and probably work — but the synthesizer's regex fallback is a public-facing fragility that will embarrass when it misfires on a new model version.

**Concrete rework estimate: 1–2 days** (8–16 hours) to:
- Convert synthesizer to strict-output API call, kill the regex (~3h)
- Move confidence rules into prompt (~1h)
- Add `nav_error` + `data_inconsistency` taxonomy types (~3h)
- Add inter-route consistency check pass to analyzer (~4h)
- Default-taxonomy refactor (~1h)
- Tests + docs (~2-3h)

---

## Forge Pass-2 scoping (no wiring)

Forge: Flask/Celery, AI tool marketplace, 5-step submission flow.

### Predicted personas

1. **Tool-submitter (technical):** mid/senior engineer publishing an AI tool. Cares about: clear requirements per step, validation feedback in-place (not bounced backwards), predictable review timeline, no lost form state on back-button / session-expire, Celery task feedback during async processing.
2. **Reviewer (judging submissions):** product/eng leadership doing batch review. Cares about: all submission info in one view, batch operations, clear queue state, ability to leave comments visible to submitter.
3. **(Optional) End-user/browser:** looking for an AI tool to install/use. Cares about: discovery (search, filter, categories), trustworthy ratings, install/usage friction. (Lower priority for Pass-2 — the submission flow is more interesting.)

### Predicted friction PersonaLab would surface

High-probability hits (PersonaLab's sweet spot):
- **Multi-step state preservation:** Flask form state lost on back-button or session-expire. Tool-submitter persona hits this on step 3, navigates away, comes back, empty form → `empty_state` or `missing_action`.
- **In-place validation feedback:** if step 2 fails validation, does the user see why on step 2 or get bounced to step 1? Common Flask anti-pattern → `navigation` friction.
- **Celery task feedback opacity:** post-submission async work without UI feedback ("Submitted, you'll get an email") → `scoring_opacity` / `empty_state` (no recovery path during the async wait).
- **Discoverability of next step:** is there a "next" button or just a redirect? → `missing_action`.
- **Step indicator clarity:** "Step 3 of 5" missing or inconsistent across steps → `navigation`.

Low-probability misses (the scope limitations):
- Celery task that silently fails server-side → PersonaLab captures `nav_error` only if the page returns 5xx; if it returns 200 with "we're processing" stuck forever, PersonaLab eventually flags `slow_load` but doesn't know it failed.
- Data integrity in the submitted record → out of scope.
- Cross-component consistency (submitted-tool fields displayed differently on submitter dashboard vs reviewer queue) → out of scope today, addressable with the rebuild-it-today item #4.

### Pass-2 setup effort

- `forge/app.yaml`: 8–15 routes (login, dashboard, submit step1–5, success, edit, browse, tool-detail), ~5 friction_signals declared, action selectors → **~2–4 hours**
- 2 personas (tool-submitter, reviewer): ~30 min each → **~1 hour**
- 1 scenario (the submission flow with a deliberately broken validation injected): ~1 hour
- First run + debugging selectors + interpreting output: ~2–3 hours

**Total: 6–9 hours** for a minimal-but-useful Forge config + first real run.

**Worth doing?** Yes. Forge is exactly the audience PersonaLab was built for — multi-persona, multi-step flows, AI-app, no obvious in-house UX function. If Pass-2 surfaces meaningful friction with this setup cost, the project's hypothesis is validated. If Pass-2 produces a polish spec full of `instrumentation_gap` or noise, the hypothesis is falsified and the recommendation flips to **fold back into career-ops as a fixture, don't publish**.

---

## The single honest recommendation

**Rework-then-ship-standalone, gated on a Forge Pass-2 that doesn't disappoint.**

Sequencing:

1. **Run Pass-2 against Forge FIRST.** 6–9 hours of setup. Decision: does the polish spec surface friction a thoughtful manual QA wouldn't have found, with low FP and minimal `instrumentation_gap` noise?
   - **Yes:** proceed to (2).
   - **No:** stop. PersonaLab is a useful career-ops fixture; **don't publish.** Fold it back; it lives as `qa/` next to a Next.js app and earns its keep there.
2. **If Pass-2 succeeds: 1–2 days of moderate rework before publish.** Synthesizer strict-output + prompt-internalized confidence rules + taxonomy expansion for `nav_error` + `data_inconsistency` + default taxonomy in framework. Address the L3 rebuild-it-today list items 1–4.
3. **Publish standalone as "PersonaLab — niche OSS UX-friction QA framework for solo/small-team AI-app builders."** Right-size the framing aggressively. Do NOT position as "AI QA platform" or "the future of testing." Position as "a useful tool for a specific audience" — closer in framing to `locust` or `radon` than to `Selenium`.
4. **Accept the realistic ceiling:** a few hundred-to-low-thousand OSS users. If a small paid SaaS materializes from that audience self-organizing, fine. Don't build for a market that isn't there.

### The fallback that needs to be said clearly

If after Pass-2 the data says "even on a non-CareerOps app this is mostly noise + setup tax," the right move is **don't publish**. Fold it back. The author would rather hear that now than after publishing — and the kill-funnel here gives a real gate (Pass-2 friction yield vs setup cost) that decides which side of the line PersonaLab lands on.

The bones are good, the irreducible value is real, the scope is honest-if-niche. But it has not yet proven it works for an app the author didn't build. **Pass-2 is the only honest test.** Everything downstream waits on its result.

---

## Files inspected (read-only)

`personalab/README.md`, `personalab/core/{runner,analyzer,scenario_runner,synthesizer,orchestrator,behavior,persona_schema,replayer,_credit_check}.py` (briefly via subagent for facts gathering), `personalab/schemas/{app-config,persona,scenario}.schema.yaml`, `qa/app.yaml`, `qa/tests/test_no_app_imports_in_core.py`, git history for the 43%→0% FP fix (commits `8aea455`, `ea1c031`, `52c0b8f`).

No code changed. No extraction performed. No Forge wiring attempted. Conceptual rereview only.
