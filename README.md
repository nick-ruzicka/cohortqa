# PersonaLab

A generic multi-persona QA framework: simulate N distinct users walking your
app, surface the friction each one hits through their own lens, and synthesize
the cross-persona patterns into a paste-ready polish spec.

PersonaLab is **app-agnostic**. The framework lives under `personalab/`;
your app's configuration lives somewhere else (e.g. `qa/` in this repo).
The same `personalab/core/*.py` modules drive different apps — you only
write a small `app.yaml` and a few `persona.yaml` files to point them at a
new project.

## The five moving parts

| Surface | What it does | Cost |
|---|---|---|
| **Runner** (`runner.py`) | Drives one persona through an app's routes in an isolated headless Chromium context. Records JSONL session logs. | Free (local Playwright). |
| **Analyzer** (`analyzer.py`) | Reads one session log, asks Claude to extract friction events through that persona's lens, writes a Markdown report. | ~$0.10 per session (Opus 4.7). |
| **Scenario runner** (`scenario_runner.py`) | Runs the same persona against baseline and a modified version of the app (DOM injections / API mocks), diffs the friction. | Free (pure structural diff). |
| **Replayer** (`replayer.py`) | Re-walks a recorded session's exact actions against the current app; reports drift in render times, missing affordances, and console errors. | Free. |
| **Synthesizer** (`synthesizer.py`) | Reads every per-persona report and produces one ranked polish spec markdown listing the top patterns + proposed fixes + S/M/L effort. | ~$0.18 per synthesis. |
| **Orchestrator** (`orchestrator.py`) | One command runs the whole loop: discover personas → run sessions in parallel → analyze → synthesize. | ≈ $0.10 × N + $0.18. |

The framework writes everything to disk. The dashboard layer (TypeScript /
React in this repo's `dashboard-web/`) reads those files. No DB, no API
contract between runner and dashboard — just JSON and Markdown files.

## Directory shape

```
personalab/                       ← framework (generic, portable)
├── core/
│   ├── persona_schema.py         ← validates app/persona/scenario YAMLs
│   ├── behavior.py               ← pure persona decision logic
│   ├── runner.py                 ← Playwright session runner
│   ├── analyzer.py               ← Claude friction extractor
│   ├── scenario_runner.py        ← DOM-inject + diff
│   ├── replayer.py               ← scripted replay + regression diff
│   ├── synthesizer.py            ← cross-persona pattern synthesis
│   └── orchestrator.py           ← CLI + run loop
├── schemas/
│   ├── app-config.schema.yaml    ← reference: shape of an app.yaml
│   ├── persona.schema.yaml       ← reference: shape of a persona.yaml
│   └── scenario.schema.yaml      ← reference: shape of a scenario.yaml
├── tests/                        ← framework unit tests
└── README.md                     ← you are here
```

Your app's configs live outside the package — see `qa/` in this repo for
CareerOps's complete example.

## How to use it on a new project

1. **Install the runtime** into a Python ≥ 3.9 venv:
   ```bash
   pip install playwright pytest pyyaml anthropic
   python -m playwright install chromium
   ```

2. **Write your app.yaml** — describes the dashboard:
   ```yaml
   app:
     name: MyApp
     dev_server: http://localhost:3000
     description: One-line description.

   routes:
     - path: /home
       purpose: Landing page.
       actions: [click_cta]
       expected_load_time_ms: 1500

   actions:
     - name: click_cta
       selector: 'button:has-text("Get Started")'
       side_effects: ["emits_event:cta.clicked"]

   friction_signals:
     - type: navigation
       description: User can't find their way back.
     # ... see personalab/schemas/app-config.schema.yaml for the full vocabulary

   personas_dir: personas
   scenarios_dir: scenarios
   runs_dir: runs
   ```

3. **Write your personas** — one YAML per simulated user:
   ```yaml
   identity:
     name: Senior Engineer
     role: Senior Engineer
     background: 5+ years at growth-stage SaaS.
   target_archetypes: [eng]
   location_preferences: [hybrid-NYC]
   comp_floor: 200000
   behavioral:
     click_speed: medium
     reads_details: true
     rejection_threshold: high
     detail_dwell_ms: 30000
   meta_attitude: Skeptical, time-pressed, knows what they want.
   friction_sensitivities: [navigation, slow_load]
   ```

4. **Run the orchestrator**:
   ```bash
   export ANTHROPIC_API_KEY=...
   python -m personalab.core.orchestrator --app path/to/app.yaml
   ```

   This walks every persona through your `dev_server`, writes session JSONLs
   to `runs/`, analyzes each into `reports/`, and writes a synthesis to
   `synthesis/polish-spec-draft-<date>.md`.

## Hard guarantees

* **Generic core firewall.** `personalab/core/*` must not import from any
  app-specific directory. Enforced by `qa/tests/test_no_app_imports_in_core.py`
  (or wherever you put the firewall test in your repo). Re-locate that test
  to your app's tests dir; it's a recipe, not a fixed location.
* **Source tagging.** Every event the runner emits carries
  `source: "personalab:<persona-id>"` so your real analytics can filter
  PersonaLab traffic out cleanly.
* **Protected actions.** Any action whose `side_effects` list contains a
  string prefixed `writes:` is *intent-logged but never clicked* by the
  runner (see `behavior.is_protected_action`). PersonaLab will not mutate
  files your app considers source-of-truth.
* **No surprise spend.** All Claude calls go through the analyzer and the
  synthesizer. The runner, scenario runner, and replayer are LLM-free.

## Tuning model + cost

The analyzer and synthesizer default to `claude-opus-4-7`. Override at
runtime with the `PERSONALAB_ANTHROPIC_MODEL` env var (e.g.
`claude-sonnet-4-6`) if you want lower-cost runs at the cost of some
analytical depth. The system prompts are unchanged — only the model swaps.

The analyzer's friction-taxonomy block carries `cache_control: ephemeral`,
so a 6-persona orchestrator run gets ~5 cache hits on the second through
sixth calls — token cost on cached tokens is ~10× lower than fresh.

## Reading the output

* **`runs/<persona>-<timestamp>.jsonl`** — raw session log. One JSON object
  per line. Replayable with `python -m personalab.core.replayer`.
* **`reports/<persona>-<timestamp>.md`** — human-readable friction report,
  sorted by severity desc.
* **`reports/<persona>-<timestamp>.json`** — the same report as structured
  data. The synthesizer reads these, not the markdown.
* **`synthesis/polish-spec-draft-<date>.md`** — the file you actually paste
  into a new Claude Code session as the input to the next polish round.

## Adapting friction signal vocabulary

The friction signal types in `personalab/core/persona_schema.py`
(`KNOWN_FRICTION_TYPES`) are deliberately generic enough to work across
dashboard-style apps. If your app needs a domain-specific type
(e.g. `payment_failure` for a checkout flow), add it to that set and
declare it in your app.yaml's `friction_signals:` block. Personas may
reference any declared type via their `friction_sensitivities:` list.

## Why a separate framework?

PersonaLab works because friction is shaped more by *who the user is* than
by *what they click*. A senior GTM Engineer's "this dashboard wastes my
time" is invisible to a generic accessibility scanner and equally invisible
to "did the page load?" health checks. Encoding the user as data lets you
re-run the same exam every time you ship, get back a per-persona report
card, and stop arguing about whose intuition was right.
