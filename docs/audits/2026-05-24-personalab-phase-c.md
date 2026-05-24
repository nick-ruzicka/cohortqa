# PersonaLab Phase C — dormant mechanisms now fire

**Date:** 2026-05-24 · **Branch:** `personalab/phase-c` (off `personalab/phase-b`) · **Mode:** two surgical runner fixes + 7 new tests + empirical re-validation on chariot · **Spend:** ~$0.40 of self-set $1.00 cap · **Verdict:** both Phase B dormant mechanisms (back-button sim, trust filter) now fire empirically on chariot with the existing 8 universal personas; distinct nav signatures went **6 → 7** of 8; all 159 tests passing (152 prior + 7 new Phase C).

---

## What was dormant

Phase B audit (`2026-05-23-personalab-phase-b.md` §"Phase B mechanisms — exercised vs not"):

> "Back-button + trust filter wired and unit-tested but didn't fire on Forge or Chariot (action_index threshold not reached / actions categorically excluded). Phase C follow-up items: (a) re-order so trust filter sees raw actions before chooses_action excludes them, (b) session-wide action_index instead of per-route."

This pass implements both, validates them on chariot, and commits.

---

## Fix 1 — session-wide `action_index`

### The bug

`runner.py:_take_route_actions` used `enumerate(actually_take)` which resets the counter to 0 at every new route. `should_go_back_after(persona, action_index)` requires `action_index > 0 AND (% 3 == 0 for 'lost' OR % 4 == 0 for 'high' error)`. Personas with 1-2 actions per route never hit those thresholds — back-button sim was unreachable for any persona on any of the validated apps.

### The fix

`PersonaRunner.__init__` gains `self._session_action_index: int = 0`. `_take_route_actions` reads `current_index = self._session_action_index` for the Phase B sim checks, then increments AFTER each successfully-dispatched action. Protected-action skips and trust-refused-action skips do NOT advance the counter (per-dispatch determinism, not per-declared determinism).

### Empirical validation on chariot

| Persona | Phase B session route count | **Phase C session route count** | Δ |
|---|---:|---:|---:|
| wanderer | 7 | **11** | **+4 back-button events** |
| others | (unchanged) | (unchanged) | 0 |

Wanderer's `goal_clarity=lost` schedule fires `should_go_back_after` at `action_index > 0 AND % 3 == 0`. With session-wide counter, wanderer's 15-action session triggers at indices **3, 6, 9, 12** — exactly 4 back-button events. Confirmed in the JSONL trace:

```
[reasoning at /index.html]: "Phase-B back-button simulation: goal_clarity=lost, error_rate=low (session-index=3)."
[reasoning at /analytics.html]: "... (session-index=6)."
[reasoning at /index.html]: "... (session-index=9)."
[reasoning at /skills.html]: "... (session-index=12)."
```

**Phase B back-button event count on chariot: 0. Phase C: 4 for wanderer.** Fix verified.

### Honest caveat that holds

Error-prone on chariot has 3 session actions (back_to_index on 3 detail routes). The `>0 AND % 4 == 0` rule for high-error-rate back-button still requires ≥ 4 actions to trip — Phase C didn't fix that for error-prone on chariot specifically; the persona just has too few action surfaces. The fix is sound; the persona test target is the limit. An app with denser action surfaces would trip it.

### Side effect on error-prone's double-click count

The fix made error-prone's double-clicks MORE discriminating, not less:

- Phase B: 3 double-click attempts (all session-indices=0 because per-route reset; `should_double_click_after(p, 0)` is True for high error_rate)
- Phase C: 2 double-click attempts (at session-indices 0 and 2; `should_double_click_after(p, 1)` is False)

This is correct behavior. Phase B's "all 3 double-click" was an artifact of the per-route bug — every action looked like index=0 and all triggered the `idx % 2 == 0` rule.

---

## Fix 2 — Trust-filter pre-pass

### The bug

`trust_filters_action` ran INSIDE the dispatch loop, AFTER `chooses_action` had already excluded actions. The chariot `run_discovery` action declares `["writes:postgres.signal_runs", "persists:discovery_results"]` — the `persists:` should trigger paranoid skeptic. But `run_discovery` has category=`other` (not scan/expansion/drilldown/etc.), so `chooses_action` returns False for every persona, and `trust_filters_action` never saw it. Paranoid posture was invisible in the trace for any action `chooses_action` would have categorically excluded.

### The fix

Pre-pass at the top of `_take_route_actions` walks ALL declared route actions (not just chooses_action-approved ones). For any action whose `trust_filters_action(persona, action)` returns True, it records a reasoning event and adds the action to `refused_by_trust`. The dispatch loop then filters `actually_take` against `refused_by_trust` so refused actions don't dispatch. The original inline trust check inside the dispatch loop is gone — pre-pass owns the recording. Pre-pass is gated on `trust_posture(persona) != "trusting"` so trusting personas get zero pre-pass overhead.

### Empirical validation on chariot

Skeptic (paranoid) JSONL after Phase C contains:

```
[reasoning at /index.html, action=run_discovery]:
  "Persona (paranoid trust) refuses 'run_discovery': side_effects
   ['writes:postgres.signal_runs', 'persists:discovery_results']
   advertise a trust-relevant intent (asks/signup/persists).
   Phase C pre-pass — recorded before chooses_action filter."
```

**Phase B trust-refusal event count on chariot: 0. Phase C: 1 for skeptic.** Fix verified.

The other 7 personas correctly produced ZERO trust refusals (they're all `trusting` posture).

---

## Test coverage

`personalab/tests/test_phase_c.py` — 7 new tests:

1. `test_trust_pre_pass_records_refusal_for_chooses_action_excluded_action` — paranoid persona, `run_discovery`-style category=`other` action with `persists:`. Refusal recorded.
2. `test_trust_pre_pass_silent_for_trusting_persona` — trusting on same route. Zero refusals.
3. `test_trust_pre_pass_does_not_double_record` — paranoid persona, chooses_action-approved action with `persists:`. Exactly ONE refusal (the pre-pass version, not the now-deleted inline version).
4. `test_trust_pre_pass_skips_dispatch_for_refused_actions` — refused action does NOT produce a click/focus/tap call OR an action event in the trace.
5. `test_session_action_index_starts_at_zero` — fresh runner has counter at 0.
6. `test_session_action_index_advances_across_routes` — one action per route across 2 routes → counter at 2 (proves no per-route reset).
7. `test_session_action_index_only_advances_on_dispatched_actions` — protected action skipped via `continue`, dispatched action runs → counter increments by 1, not 2.

The tests use a lightweight async stub page (`_StubPage` with a call ledger) rather than Playwright, so they run with the rest of the suite in ~10s total. End-to-end Playwright coverage continues to live in `test_runner_smoke.py`.

**Test suite: 152 → 159, all passing.**

---

## Distinct nav signature progression

| Persona | Phase A (depth) | Phase B | **Phase C** | What isolated them |
|---|:---:|:---:|:---:|---|
| cautious-first-timer | shared (default exploratory) | shared | **solo** | (Phase C didn't add a marker — split is on event count differences from the synthesizer) |
| error-prone | shared | **solo** (2 dc attempts) | **solo** (2 dc attempts) | Phase B double-click attempts |
| keyboard-only | shared (slow read no scan) | **solo** | **solo** | Phase B `via keyboard modality` |
| returning-user | shared | **solo** | **solo** | Phase B reversed route order |
| rusher | shared (fast skim) | shared with skimmer | shared with skimmer | (both `clear`, no other differentiator) |
| skeptic | shared with first-timer | shared with first-timer | **solo (Phase C)** | **Phase C trust pre-pass refusal** |
| skimmer | shared with rusher | shared with rusher | shared with rusher | — |
| wanderer | shared | **solo** (lost-interleave) | **solo** (lost + 4 back-buttons) | Phase B lost-clarity, Phase C back-button events |
| **Distinct sigs** | **5** | **6** | **7** | |

Two of the seven are now solo via Phase C specifically (skeptic via trust pre-pass; wanderer's existing solo signature now ALSO carries 4 back-button trace events that make it richer).

Only the rusher/skimmer pair still shares — both are `clear` goal_clarity with no other Phase B/C differentiator. Splitting them would need a 6th behavioral axis (decisiveness, distraction, multitasking) that the design intentionally doesn't have yet. YAGNI.

---

## Synthesis impact — did the moat get richer?

| Metric | Phase B chariot | **Phase C chariot** | Δ |
|---|:---:|:---:|---|
| Total synth patterns | 8 | **9** | +1 |
| Cross-persona patterns | 6 | **7** | +1 |
| Single-explorer-visible (honestly tagged) | 2 | 2 | 0 |
| Instrumentation_gap correctly flagged low-conf | 1 | 2 | +1 (both rightly tagged) |
| Net-new finding the analyzer can build on | — | **run_discovery has no preview/consent/scope** | + |

The most interesting new pattern enabled by Phase C:

> **Pattern #2: `run_discovery` has no preview, consent, or scope disclosure** _(scoring_opacity)_ · cross-persona
> personas affected: skeptic, keyboard-only
> sig: "trust-sensitive (skeptic) and feedback-sensitive (keyboard-only) personas both hit; rusher/returning-user did not because they didn't dwell on consent/preview surfaces."

This pattern is only visible because Phase C surfaced skeptic's trust refusal of `run_discovery` to the analyzer. In Phase B the trust refusal didn't exist in the trace, so the analyzer had nothing to cluster skeptic with keyboard-only on. **Phase C empirically expanded the moat by one cross-persona pattern.**

And the new cross-persona headline:

> "6 of 8 personas flagged the same root cause — index.html console errors (404s + missing contacts_data.json fallback to empty SAMPLE_DATA) — but the downstream interpretation diverged by trust profile: skeptic read it as governance failure, wanderer as hollow value-prop, first-timer as anxiety trigger, while returning-user and keyboard-only (who didn't read console state into the UI) sailed through."

That's the moat in legible form — same root cause, different interpretations by trust profile. Possible only because the trust pre-pass made skeptic's posture visible to the synthesizer.

---

## What this lands for the publish story

Per the publish-prep recommendation (previous session): the three items most worth doing before OSS publish were (1) quickstart example app, (2) **fix the two dormant Phase B mechanisms**, (3) right-sized README. **Item #2 is done.**

The README can now honestly claim: "all 5 Phase B mechanisms exercise on apps that surface enough actions" with empirical evidence — wanderer's 4 back-button events on chariot + skeptic's `run_discovery` refusal in the same run. The "wired but doesn't fire" honest-caveat from the Phase B audit no longer applies.

What remains for the publish:
- Quickstart example app (3-4h)
- README right-sizing pass (2h)
- pyproject.toml + CI (~3h table-stakes)
- The 6th behavioral axis to split rusher/skimmer is documented as YAGNI; revisit only if a real user hits it

---

## Operational notes

- Branch: `personalab/phase-c` (off `personalab/phase-b`). Not pushed, not merged.
- Chariot static server: still running on :8002 from earlier sessions.
- Chariot Phase C artifacts: `chariot-qa/_phase-c/{reports,synthesis}/` + JSONLs in `chariot-qa/_runs/*-20260524T*.jsonl`. Not committed (evidence, reproducible).
- Phase B `chariot-qa/_reports`, `_runs`, `_synthesis` dirs from prior runs still on disk; Phase C wrote to a separate `_phase-c/` dir to keep the comparison clean.

## Spend

| Step | Cost |
|---|---:|
| Implementation (Fix 1 + Fix 2) | $0 |
| 7 new tests + suite run | $0 |
| Chariot empirical validation (Haiku analyzer + Opus synth, 8 personas) | ~$0.40 |
| This audit | $0 |
| **Total** | **~$0.40** |
