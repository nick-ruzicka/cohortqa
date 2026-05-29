# CohortQA quickstart — MyTodos

A 5-step, ~5-minute first-run that points CohortQA at a small
deliberately-imperfect 4-page TODO app and produces a polish spec
listing the friction issues 8 different personas surfaced.

**What you should see at the end:** a markdown document like the one
in `_expected/polish-spec-sample.md` (captured from a real run on
this app), with a cross-persona headline at the top and four
patterns the personas converged on. **Cost ~$0.40** with the
Haiku-analyzer / Opus-synthesizer split.

If anything in these 5 steps takes more than 5 minutes for you (the
first time, with no CohortQA background), that's a quickstart bug —
please open an issue.

---

## 0. Prereqs

- **Python 3.9+** (3.11+ recommended). `python3 --version`.
- **An Anthropic API key.** Free-tier credits are enough for one run.
  Get one at <https://console.anthropic.com/>.
- About 200 MB of disk for Chromium (Playwright's headless browser).

You do **not** need: Docker, Node, a Supabase project, OpenAI, or
anything else. The TODO app is plain static HTML served by Python.

---

## 1. Install Playwright + the framework's Python deps  *(~90s)*

From the repository root:

```bash
python3 -m pip install playwright pyyaml anthropic pydantic
python3 -m playwright install chromium
```

The chromium download is ~90 MB. (Once `pyproject.toml` lands you'll
be able to skip this with `pip install cohortqa[examples]`.)

---

## 2. Serve the TODO app on `localhost:8765`  *(~5s)*

In one terminal, from the repo root:

```bash
cd cohortqa/examples/quickstart/site
python3 -m http.server 8765
```

That's it — the app is plain HTML, no build step. Leave this terminal
running. Open <http://localhost:8765/> in a browser if you want to see
what the personas will be walking.

---

## 3. Set your API key + (recommended) the Haiku/Opus model split  *(~5s)*

In a **second** terminal, also from the repo root:

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# Recommended: use Haiku for per-session friction labeling (cheap),
# Opus for the cross-persona synthesis (where the moat is). Cuts cost
# from ~$1.00 to ~$0.40 with no observable label-quality regression
# (validated in docs/audits/2026-05-23-personalab-depth-design.md §2).
export COHORTQA_ANALYZER_MODEL=claude-haiku-4-5-20251001
export COHORTQA_SYNTHESIZER_MODEL=claude-opus-4-7
```

If you skip the model split, CohortQA uses Opus for both stages —
still works, just costs 2-3× more.

---

## 4. Run the orchestrator  *(~2-3 min wall, ~$0.40)*

Still in the second terminal, from the repo root:

```bash
python3 -m cohortqa.core.orchestrator \
  --app cohortqa/examples/quickstart/app.yaml \
  --parallel 8 \
  --reports-dir cohortqa/examples/quickstart/_reports \
  --synthesis-dir cohortqa/examples/quickstart/_synthesis
```

You'll see 8 personas walk the four routes in parallel headless
Chromium contexts. Total elapsed time ~140 s on a 2024 Mac. The
orchestrator prints a summary table on completion.

---

## 5. Read the polish spec  *(~1 min)*

```bash
cat cohortqa/examples/quickstart/_synthesis/polish-spec-draft-*.md
```

The document opens with a **cross-persona headline** — the one
insight that's only visible by comparing N personas — followed by 4
patterns ranked by impact. On a fresh run against this app, you
should see roughly:

> **All 8 of 8 personas hit the same three friction points (no back
> link on /add.html, no back link on /detail.html, dead-end
> /done.html) — this is universal navigation-architecture friction,
> not persona-specific; the absence of any persona who succeeded
> means there is no comparative trait to isolate.**

Plus a pattern for the trust-paranoid skeptic refusing the
`persists:`-tagged Save button (the framework's Phase C trust filter
in action), and a medium-confidence `scoring_opacity` finding on the
unexplained priority chip.

**That's a complete quickstart.** Stop the static server with
`Ctrl-C` in the first terminal.

---

## What was actually demonstrated

In about 5 minutes you've exercised:

- The **runner**: 8 deterministic Playwright sessions, one per persona,
  with per-persona behavioral divergence (mouse vs keyboard modality,
  lost-route interleave, clear-route truncation, error-prone double-
  click attempts).
- The **analyzer**: 8 Claude calls (cheap Haiku) labeling each session's
  friction events against the app-declared signal taxonomy.
- The **synthesizer**: one Claude call (strong Opus) clustering across
  persona reports for cross-persona patterns. The cross-persona
  headline is the moat — it's the kind of finding a single confused
  user walking the app couldn't structurally produce.
- The **Phase B/C runtime mechanisms** that distinguish CohortQA from
  "headless Chromium + N prompts": modality dispatch, route ordering
  by goal-clarity, error simulation, trust-posture pre-pass. See
  `docs/audits/2026-05-24-personalab-phase-c.md` for empirical proof
  these fire.

The whole framework's source is in `cohortqa/core/`. Friction
taxonomy is declared per-app in `app.yaml`. Personas are YAML files
you copy from `cohortqa/personas/`.

---

## Adapting this for your own app

The quickstart is designed to be `cp -r`-able onto a new project:

1. Copy this directory: `cp -r cohortqa/examples/quickstart ~/my-app-qa`.
2. Replace `site/` with your app (or point `app.dev_server` in
   `app.yaml` at wherever your app already runs locally).
3. Rewrite `app.yaml`'s `routes`, `actions`, and `friction_signals`
   sections to match your app's surfaces. The selectors use Playwright
   locator syntax (`#id`, `.class`, `[attr]`, `:has-text(...)`).
4. Keep the 8 universal personas as-is — they're posture-defined, not
   domain-specific. Customize `identity.background` if you want the
   analyzer to lens findings through your specific user shape.
5. Re-run step 4 from above. CohortQA will produce a polish spec
   for your app within a few minutes.

A realistic first-time setup for a new app is **15-25 minutes**:
the time goes into authoring `app.yaml` (mapping your routes +
selectors), not into wrestling with the framework.

---

## What could go wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'cohortqa'` | Running from the wrong directory | Run from the repo root, not from `cohortqa/examples/quickstart/` |
| `playwright._impl._errors.Error: Executable doesn't exist` | Chromium not installed for Playwright | `python3 -m playwright install chromium` |
| `anthropic.AuthenticationError` | `ANTHROPIC_API_KEY` not set or invalid | Re-`export` it; verify with `echo $ANTHROPIC_API_KEY` |
| Orchestrator times out on every route | Static server isn't running | Restart `python3 -m http.server 8765` from `site/` |
| `BadRequestError: 'adaptive thinking is not supported on this model'` | An unusual analyzer model env override | The Haiku gate covers Haiku 4.5; older Haiku versions don't support adaptive thinking. Stick to `claude-haiku-4-5-20251001` |
| Polish spec is empty / has zero patterns | Personas couldn't reach the app | Verify `curl http://localhost:8765/index.html` returns HTML before re-running |

---

## What this example deliberately does NOT do

- **No backend.** The TODO app is static HTML — there's no real
  database, no auth, no API. The personas walk the rendered surface;
  none of the protected actions (`save_todo`, `mark_done`,
  `delete_todo`) are ever actually clicked (their `writes:` side
  effects trigger CohortQA's protected-action suppression).
- **No scenarios.** CohortQA supports DOM injections / scenario
  variants via `scenario_runner.py`; the quickstart skips them to
  keep the surface minimal. The `personas/` dir is the one cap-
  customization point.
- **No persistence across runs.** Each orchestrator invocation writes
  to fresh `_runs/` `_reports/` `_synthesis/` dirs. Re-running over-
  writes them. (For a real app you'd keep these in git or treat them
  as build artifacts.)

---

## Where the cost goes

Per the validated cost-architecture in
`docs/audits/2026-05-23-personalab-depth-design.md` §2:

| Stage | Cost on this run |
|---|---:|
| Runner (Playwright, no API) | $0.00 |
| 8 × analyzer (Haiku, cached friction-taxonomy block) | ~$0.10 |
| 1 × synthesizer (Opus) | ~$0.18 |
| **Total** | **~$0.28-0.40** |

The naive same-quality run (Opus everywhere) is ~$1.00. The split is
opt-in via env vars and validated to produce no measurable label
quality regression on the apps CohortQA has been tested against.

---

## What to read next

- **The audits** under `docs/audits/2026-05-2*-cohortqa-*.md` — the
  full validation history (Pass-2 / rework / depth / Phase B / Phase
  C) is in there, including honest caveats and what didn't work.
- **The framework code** under `cohortqa/core/` — the runner is
  766 lines of Playwright + behavioral rules; the analyzer + synthesizer
  are ~500 lines each of prompt construction + structured Claude calls.
- **The 8 universal personas** under `cohortqa/personas/` — these
  are the load-bearing library. Read the `meta_attitude` blocks to
  understand what each persona surfaces.
