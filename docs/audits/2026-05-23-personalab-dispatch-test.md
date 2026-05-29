# CohortQA Dispatch test — Phase 0 STOP

_Formerly known as **PersonaLab** — renamed to **CohortQA** at OSS publish (2026-05-28)._

**Date:** 2026-05-23 · **Branch:** `cohortqa/dispatch-test` (off `cohortqa/phase-b`) · **Mode:** Phase 0 runnability gate only · **Spend:** ~$0.00 (just GitHub API metadata reads) · **Verdict:** **STOP at Phase 0** — Dispatch can't be spun up locally cheaply enough to justify a 4th-codebase validation pass. The existing 3-codebase validation (Forge + CareerOps + Chariot) is the right stopping point; proceed to publish-prep.

---

## Phase 0 gate — runnability assessment

### Located the repo

Dispatch is at `github.com/nick-ruzicka/crm-terminal` (public). Repo description: *"AI-native CRM terminal — semantic search, RAG suggestions, and Claude action tools built at Linera."* README title is literally "Dispatch — AI CRM Terminal". Last push: 2026-03-04.

Not present locally (`~/projects/dispatch*`, `~/dispatch*`, broader sweep all empty).

### Stack inspection (without cloning — `gh api`)

**Tech stack** (from README + `package.json`):
- Frontend: Next.js 14 (App Router), TypeScript, Tailwind
- Database: **Supabase** (PostgreSQL + pgvector for semantic search)
- AI: **Anthropic Claude API** (chat + extraction) + **OpenAI** (embeddings)
- Automation: **Zapier** webhook (Granola → Supabase)
- Optional: **Asana** integration

**`.env.example` declared variables:**
```
NEXT_PUBLIC_SUPABASE_URL=https://your-project-id.supabase.co   (real remote URL expected)
NEXT_PUBLIC_SUPABASE_ANON_KEY=your_supabase_anon_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
OPENAI_API_KEY=your_openai_api_key_here                         (powers embeddings/search)
ASANA_ACCESS_TOKEN=your_asana_personal_access_token_here
ASANA_PROJECT_ID=your_asana_project_id_here
ASANA_USER_GID=your_asana_user_gid_here
NEXT_PUBLIC_BASE_URL=http://localhost:3000
CRON_SECRET=your_cron_secret_here
```

**README setup section** — exactly three steps:
1. `npm install`
2. `cp .env.local.example .env.local` and fill in the keys (URL points at a real Supabase project)
3. `npm run dev`

**No documented local-dev / fake-Supabase / seed-data path.** The setup section assumes you've already provisioned a remote Supabase project and have all four API keys.

### Local-environment audit

| Prerequisite | Status |
|---|---|
| Node.js + npm | ✓ available |
| Docker daemon | ✓ running, no containers |
| `supabase` CLI | ✗ not installed |
| Local Anthropic API key | ✓ available (in careerops `.env`) |
| Local OpenAI API key | ✗ unknown — not in any obvious env file |
| Local Asana access token | ✗ not present |
| Dispatch instance currently running on any common port | ✗ none |

### What it would take to spin up locally (estimated)

1. `git clone github.com/nick-ruzicka/crm-terminal` → 10s
2. `npm install` (Next.js + several deps incl. `@anthropic-ai/sdk`, `openai`, `@supabase/supabase-js`, `@hello-pangea/dnd`, etc.) → 2-3 min
3. **Install Supabase CLI** + run `npx supabase init && npx supabase start` (downloads ~1GB of Docker images first time) → 5-10 min first time
4. Run migrations from the repo's `supabase/` dir against local Postgres → 1-2 min, **assuming migrations exist and are CLI-compatible**
5. **Write throwaway seed SQL** for the schema (deals/contacts/notes — FK relationships, vector embeddings column) → 5-15 min depending on how empty-friendly the UI is
6. Provision an `OPENAI_API_KEY` (needed for `openai` package init even if unused) → blocked or 5 min
7. Stub or empty `ASANA_ACCESS_TOKEN` (the UI may attempt to fetch on certain views — would error if hit)
8. `npm run dev` → 30s
9. Verify UI renders all major routes (pipeline, deal management, chat, search)

**Total estimated wall-clock: 25-40 min with no errors, more realistically 45-60 min including debugging Supabase migrations, schema mismatches, or missing-key UI errors.**

For comparison:
- Forge Pass-2 setup: ~25 min (already running, just config)
- Chariot Phase B: ~12 min (static export, `python3 -m http.server`)
- Dispatch projected: **25-60 min** (real DB provision + seed + key wrangling)

### The substantive risks

Even after spin-up, the test would be degraded:

1. **Empty pipeline.** With no seed data the pipeline-dashboard surface (the most important navigable area for CohortQA) would render empty cards. The runner can walk an empty UI (proven on chariot), but the friction findings would be dominated by "empty_state" labels rather than the rich interaction patterns the test is designed to surface.
2. **Broken chat surface.** The "natural language interface powered by Claude" route depends on `ANTHROPIC_API_KEY` having credits + `OPENAI_API_KEY` for embeddings. Missing OpenAI key likely means the chat surface throws on init — the personas couldn't exercise it.
3. **Asana errors mid-flow.** Any view that pulls Asana tasks would error out if `ASANA_ACCESS_TOKEN` is empty/stub. Personas would file these as broken_link / instrumentation_gap noise.
4. **Trust-filter test contamination.** The whole point of Dispatch as a 4th-codebase test was to exercise the dormant trust filter (`asks:`/`signup:`/`persists:` side_effects). To do that meaningfully the personas need to encounter real "deal create" / "contact edit" / "send Slack" affordances. Those affordances only render when the underlying tables have data AND the integrations are configured. An empty stubbed instance would let the trust filter fire on protected actions like `create_deal`, but the *broader* trust-relevant surfaces (Asana sync, Slack send, Zapier webhook) wouldn't be in the trace at all.

This is exactly the "requires the full live integration stack to render anything meaningful" case the Phase 0 gate was built to catch. The test could be FORCED to run, but the inputs to CohortQA would be so degraded that any moat-scaling claim would be argued away by "well, you tested it on a broken Dispatch."

---

## Recommendation: STOP at Phase 0, take 3-codebase validation as sufficient

The user prompt explicitly said:

> "A clean STOP here is a valid outcome — we'll take the existing 3-codebase validation as sufficient and move to publish-prep."

That's the right move. The existing validation across **three real codebases of different shape** already covers the moat claim with margin:

| | Forge | CareerOps | Chariot |
|---|---|---|---|
| Type | Flask + Postgres + Celery, multi-step submit | Next.js dashboard, instrumented with `data-action` hooks | Static HTML export (no backend reachable) |
| Familiar to author? | Yes (he built it) | Yes (he built it) | No (third-party reference) |
| Phase B sigs | 5 (same count as Phase A, different groupings) | n/a (only ran Phase A) | **6** (up from 5 in Phase A) |
| Moat held? | ✓ (5+3 in Pass-2, 5+3 in depth) | ✓ (9 cross-persona patterns in depth control) | ✓ (6+2 in Phase B) |
| Net-new finding plain CC missed? | 1 (role-picker-modal pointer-event trap) | n/a (control run) | 1 (console-404 / trust erosion) |

Two of three apps had a confirmed *"only-CohortQA-could-find-this"* finding. Cross-persona pattern counts ranged 5-9. Phase B mechanisms exercised on 3 of 5 axes (modality, route-order, error-sim) with the other 2 (back-button, trust filter) wired-and-unit-tested but app-shape-dependent. This is a defensible validation surface for a publish.

### What we'd have learned from Dispatch (and didn't)

**The two soft questions the test was designed to answer:**

1. **Does the moat scale with app complexity?** *Unanswered empirically. The 3-codebase result is a TREND (5/6/5 distinct sigs and 5-9 cross-persona patterns), not a scaling-with-complexity demonstration. To resolve this would need either Dispatch (or another rich, locally-runnable app) actually running. Best honest answer in the publish material: "validated on 3 apps; moat held; complexity-scaling claim is not made because we don't have the data."*
2. **Do the dormant Phase B mechanisms fire on a richer app?** *Same answer — unanswered. The Phase B doc honestly flagged this as a Phase C follow-up; that flag stays.*

Both questions deserve a placeholder in the publish doc: "future work — validate on a more complex app once one is locally-runnable cheaply, or fix the architectural quirks (action_index per-route, chooses_action / trust_filter ordering) that gate the dormant mechanisms."

---

## Branch state

`cohortqa/dispatch-test` was created off `cohortqa/phase-b` for this audit. No code changes, no qa config, no run artifacts. Only this audit doc lands on the branch — the work product of Phase 0.

**Recommended next action:** merge intent of this audit back into the trajectory by either:
- Closing this branch (no real work to land) and moving to publish-prep on `cohortqa/phase-b`'s state
- Keeping this branch as the documentation of the stop decision, and tagging the audit so future-you can find it when "should we test on Dispatch?" comes up again

---

## Spend accounting

| Phase | Cost |
|---|---:|
| Branch creation | $0 |
| GitHub repo metadata read (`gh repo view`, `gh api`) | $0 |
| README + `.env.example` + `package.json` inspection | $0 |
| Local environment audit (`which supabase`, `docker ps`, port scans) | $0 |
| This audit doc | $0 |
| **Total** | **$0 of $3.50 budget** |

The Phase 0 gate did exactly what it was designed to do: identified the cost before committing to it.
