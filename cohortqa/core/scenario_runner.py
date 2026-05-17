"""Scenario runner — prospective testing of hypothetical app changes.

A scenario describes a modification ("what if we added X to /pipeline?"),
the ScenarioRunner runs the *same persona* against baseline and scenario
versions of the app, then computes a structural diff (action counts,
render times, missing-affordance gaps, console errors). The diff report
goes to ``qa/scenarios-results/``.

The diff is pure — no Claude call. Cost stays at 0 for prospective
testing; only the optional analyzer pass on each side spends tokens.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .runner import PersonaRunner, read_session


# ─── Scenario → Playwright context_hook ──────────────────────────────────────

def make_scenario_context_hook(scenario: dict[str, Any]):
    """Return an async ``context_hook`` that applies the scenario's
    modifications to the BrowserContext.

    Supported modifications:

    * ``dom_injections`` — for each matching route, inject HTML into the
      first element matching ``selector`` (after the page loads).
    * ``api_mocks`` — intercept matching endpoints, fulfill with the
      mock JSON body.

    ``config_overrides`` are not applied here — they're meant for
    app-level config files and would require runner-side cooperation.
    The scenario validator accepts them; the runner ignores them with
    a warning. (P5 scope; planned for P9 orchestrator if needed.)
    """
    mods = scenario.get("modifications") or {}
    dom_injs = mods.get("dom_injections") or []
    api_mocks = mods.get("api_mocks") or []
    config_overrides = mods.get("config_overrides") or []

    async def hook(context):
        if api_mocks:
            await _install_api_mocks(context, api_mocks)
        if dom_injs:
            _install_dom_injections(context, dom_injs)
        if config_overrides:
            # Surfacing in a console.warn so it's visible during runs and
            # in the runner's captured console_errors list.
            await context.add_init_script(
                "console.warn('[personalab] scenario config_overrides "
                "not yet applied at runner level — ignoring')"
            )

    return hook


async def _install_api_mocks(context: Any, mocks: list[dict[str, Any]]) -> None:
    """Register one route handler per mock. Most-specific endpoint string
    wins by registration order (Playwright fires handlers LIFO)."""

    async def make_handler(mock: dict[str, Any]):
        body = json.dumps(mock["response"])

        async def handler(route, request):
            await route.fulfill(
                status=200, content_type="application/json", body=body
            )
        return handler

    # Wildcard pattern that matches everything; we filter inside the handler
    # so mocks can match by substring rather than full URL.
    async def dispatcher(route, request):
        url = request.url
        for m in mocks:
            if m["endpoint"] in url:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(m["response"]),
                )
                return
        await route.fallback()

    await context.route("**/*", dispatcher)


def _install_dom_injections(context: Any, injections: list[dict[str, Any]]) -> None:
    """Attach a per-page listener that injects HTML on matching routes.

    Uses the page's ``load`` event so the target selector is reliably
    present by the time we query for it.
    """

    def on_page(page):
        async def on_load():
            try:
                url_path = urlparse(page.url).path or "/"
            except Exception:  # noqa: BLE001
                return
            for inj in injections:
                if inj.get("route") != url_path:
                    continue
                selector = inj["selector"]
                html = inj["html"]
                try:
                    await page.evaluate(
                        """
                        ({selector, html}) => {
                            const t = document.querySelector(selector);
                            if (t) t.insertAdjacentHTML('beforeend', html);
                        }
                        """,
                        {"selector": selector, "html": html},
                    )
                except Exception:  # noqa: BLE001
                    # An injection failure shouldn't crash the run — the
                    # diff will show no scenario-side change.
                    pass

        page.on("load", lambda: asyncio.create_task(on_load()))

    context.on("page", on_page)


# ─── Diff computation (pure, no Playwright) ──────────────────────────────────

@dataclass
class RouteSlice:
    """Aggregate stats for one route within one session."""
    route: str
    nav_count: int = 0
    action_count: int = 0
    render_time_ms_avg: int = 0
    slow_count: int = 0
    missing_affordance_count: int = 0
    suppressed_action_count: int = 0
    console_errors: int = 0


def summarize_session(events: list[dict[str, Any]]) -> dict[str, RouteSlice]:
    """Aggregate per-route stats from a session's events. Returned keyed
    by route path; routes not visited are simply absent."""
    by_route: dict[str, RouteSlice] = {}
    render_acc: dict[str, list[int]] = {}
    last_route_console: dict[str, int] = {}

    for ev in events:
        route = ev.get("route")
        if not route:
            continue
        slc = by_route.setdefault(route, RouteSlice(route=route))
        et = ev.get("event_type")
        if et == "nav":
            slc.nav_count += 1
            rt = ev.get("render_time_ms")
            if isinstance(rt, int):
                render_acc.setdefault(route, []).append(rt)
            ps = ev.get("page_state") or {}
            cerr = ps.get("console_errors") or []
            slc.console_errors = max(slc.console_errors, len(cerr))
            last_route_console[route] = len(cerr)
            # Slow load = render exceeded the route's budget; we infer
            # it from the reasoning text the runner emits ('; SLOW').
            reasoning = ev.get("reasoning") or ""
            if "; SLOW" in reasoning:
                slc.slow_count += 1
        elif et == "action":
            slc.action_count += 1
        elif et == "reasoning":
            txt = (ev.get("reasoning") or "")
            if "no matching affordance" in txt:
                slc.missing_affordance_count += 1
            elif "click suppressed" in txt:
                slc.suppressed_action_count += 1

    for route, samples in render_acc.items():
        if samples:
            by_route[route].render_time_ms_avg = sum(samples) // len(samples)

    return by_route


@dataclass
class RouteDiff:
    route: str
    actions_delta: int          # scenario - baseline
    render_ms_delta: int        # scenario - baseline (avg)
    slow_delta: int
    missing_delta: int
    console_errors_delta: int


def compute_diff(
    baseline_events: list[dict[str, Any]],
    scenario_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Structural diff between two session event streams."""
    base = summarize_session(baseline_events)
    scen = summarize_session(scenario_events)
    routes = sorted(set(base.keys()) | set(scen.keys()))
    per_route: list[RouteDiff] = []
    for r in routes:
        b = base.get(r) or RouteSlice(route=r)
        s = scen.get(r) or RouteSlice(route=r)
        per_route.append(RouteDiff(
            route=r,
            actions_delta=s.action_count - b.action_count,
            render_ms_delta=s.render_time_ms_avg - b.render_time_ms_avg,
            slow_delta=s.slow_count - b.slow_count,
            missing_delta=s.missing_affordance_count - b.missing_affordance_count,
            console_errors_delta=s.console_errors - b.console_errors,
        ))

    totals = {
        "baseline_actions": sum(v.action_count for v in base.values()),
        "scenario_actions": sum(v.action_count for v in scen.values()),
        "baseline_missing": sum(v.missing_affordance_count for v in base.values()),
        "scenario_missing": sum(v.missing_affordance_count for v in scen.values()),
        "baseline_errors": sum(v.console_errors for v in base.values()),
        "scenario_errors": sum(v.console_errors for v in scen.values()),
    }
    return {"routes": [d.__dict__ for d in per_route], "totals": totals}


def render_diff_markdown(
    scenario: dict[str, Any],
    persona_id: str,
    diff: dict[str, Any],
    baseline_session_path: str,
    scenario_session_path: str,
) -> str:
    """Human-readable scenario diff."""
    totals = diff["totals"]
    routes = diff["routes"]

    def _delta(n: int) -> str:
        if n > 0:
            return f"+{n}"
        return str(n)

    lines = [
        f"# Scenario diff — {scenario['name']}",
        f"_persona: `{persona_id}` · generated: {_iso_now()}_",
        "",
        f"**Description:** {scenario['description']}",
        f"**Based on:** `{scenario['based_on']}`",
        "",
        f"- Baseline session: `{baseline_session_path}`",
        f"- Scenario session: `{scenario_session_path}`",
        "",
        "## Totals",
        f"- Actions: {totals['baseline_actions']} → {totals['scenario_actions']} "
        f"({_delta(totals['scenario_actions'] - totals['baseline_actions'])})",
        f"- Missing affordances: {totals['baseline_missing']} → "
        f"{totals['scenario_missing']} "
        f"({_delta(totals['scenario_missing'] - totals['baseline_missing'])})",
        f"- Console errors: {totals['baseline_errors']} → "
        f"{totals['scenario_errors']} "
        f"({_delta(totals['scenario_errors'] - totals['baseline_errors'])})",
        "",
        "## Per-route deltas",
        "| Route | Δ actions | Δ render ms | Δ slow | Δ missing | Δ errors |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in routes:
        lines.append(
            f"| `{r['route']}` | {_delta(r['actions_delta'])} | "
            f"{_delta(r['render_ms_delta'])} | {_delta(r['slow_delta'])} | "
            f"{_delta(r['missing_delta'])} | {_delta(r['console_errors_delta'])} |"
        )
    return "\n".join(lines) + "\n"


# ─── ScenarioRunner ──────────────────────────────────────────────────────────

class ScenarioRunner:
    """Run a persona through baseline + scenario, write the diff report."""

    def __init__(
        self,
        app_config: dict[str, Any],
        persona: dict[str, Any],
        persona_id: str,
        scenario: dict[str, Any],
        runs_dir: str | Path,
        scenarios_results_dir: str | Path,
        headless: bool = True,
        dev_server_override: str | None = None,
    ) -> None:
        self.app_config = app_config
        self.persona = persona
        self.persona_id = persona_id
        self.scenario = scenario
        self.runs_dir = Path(runs_dir)
        self.scenarios_results_dir = Path(scenarios_results_dir)
        self.headless = headless
        self.dev_server_override = dev_server_override

    async def run_baseline(self) -> dict[str, Any]:
        runner = PersonaRunner(
            persona=self.persona,
            persona_id=f"{self.persona_id}-baseline",
            app_config=self.app_config,
            runs_dir=self.runs_dir,
            headless=self.headless,
            dev_server_override=self.dev_server_override,
        )
        return await runner.run()

    async def run_scenario(self) -> dict[str, Any]:
        hook = make_scenario_context_hook(self.scenario)
        runner = PersonaRunner(
            persona=self.persona,
            persona_id=f"{self.persona_id}-scenario-{_slugify(self.scenario['name'])}",
            app_config=self.app_config,
            runs_dir=self.runs_dir,
            headless=self.headless,
            dev_server_override=self.dev_server_override,
            context_hook=hook,
        )
        return await runner.run()

    async def run_both(self) -> dict[str, Any]:
        baseline_summary = await self.run_baseline()
        scenario_summary = await self.run_scenario()

        baseline_events = read_session(baseline_summary["session_path"])
        scenario_events = read_session(scenario_summary["session_path"])
        diff = compute_diff(baseline_events, scenario_events)

        self.scenarios_results_dir.mkdir(parents=True, exist_ok=True)
        out_path = (
            self.scenarios_results_dir
            / f"{_slugify(self.scenario['name'])}-{self.persona_id}-{_timestamp_slug()}.md"
        )
        out_path.write_text(
            render_diff_markdown(
                self.scenario,
                self.persona_id,
                diff,
                baseline_summary["session_path"],
                scenario_summary["session_path"],
            ),
            encoding="utf-8",
        )
        return {
            "baseline": baseline_summary,
            "scenario": scenario_summary,
            "diff": diff,
            "report_path": str(out_path),
        }


# ─── Utility ──────────────────────────────────────────────────────────────────

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slugify(s: str) -> str:
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "_", "-"):
            out.append("-")
    return "".join(out).strip("-") or "scenario"


__all__ = [
    "ScenarioRunner",
    "make_scenario_context_hook",
    "compute_diff",
    "summarize_session",
    "render_diff_markdown",
]
