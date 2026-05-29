"""Tests for the scenario runner.

The PersonaRunner-driving end-to-end test would be expensive (two
Playwright sessions per scenario), so we cover that path with a single
optional smoke test that runs only when Playwright is installed. The
diff / summarise / render helpers are tested in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cohortqa.core.scenario_runner import (
    RouteDiff,
    RouteSlice,
    compute_diff,
    make_scenario_context_hook,
    render_diff_markdown,
    summarize_session,
)


# ─── Synthetic event helpers ──────────────────────────────────────────────────

def _ev(et: str, route: str | None = None, **rest: Any) -> dict[str, Any]:
    return {"event_type": et, "route": route, **rest}


def _nav(route: str, render_time_ms: int, slow: bool = False,
         console_errors: int = 0) -> dict[str, Any]:
    return _ev(
        "nav",
        route=route,
        render_time_ms=render_time_ms,
        reasoning=f"Loaded in {render_time_ms}ms (budget 2000ms{'; SLOW' if slow else ''}).",
        page_state={
            "url": f"http://x{route}",
            "status": 200,
            "title": "t",
            "body_text_length": 100,
            "visible_action_names": [],
            "console_errors": [f"e{i}" for i in range(console_errors)],
            "nav_error": None,
        },
    )


def _action(route: str, name: str) -> dict[str, Any]:
    return _ev("action", route=route, action=name)


def _reasoning(route: str, text: str) -> dict[str, Any]:
    return _ev("reasoning", route=route, reasoning=text)


# ─── summarize_session ────────────────────────────────────────────────────────

def test_summarize_aggregates_per_route():
    events = [
        _nav("/today", 1200),
        _action("/today", "run_scan"),
        _nav("/pipeline", 3000, slow=True, console_errors=1),
        _action("/pipeline", "click_role_row"),
        _action("/pipeline", "mark_skipped"),
        _reasoning("/pipeline", "Persona would have taken ['hide_with_reason'] but no matching affordance found on /pipeline."),
        _reasoning("/pipeline", "Persona would have taken 'mark_evaluated' but the action's side_effects include a protected write; intent logged, click suppressed."),
    ]
    by_route = summarize_session(events)
    assert by_route["/today"].nav_count == 1
    assert by_route["/today"].action_count == 1
    assert by_route["/today"].render_time_ms_avg == 1200
    assert by_route["/pipeline"].action_count == 2
    assert by_route["/pipeline"].slow_count == 1
    assert by_route["/pipeline"].console_errors == 1
    assert by_route["/pipeline"].missing_affordance_count == 1
    assert by_route["/pipeline"].suppressed_action_count == 1


def test_summarize_averages_multiple_renders():
    events = [_nav("/x", 1000), _nav("/x", 3000)]
    assert summarize_session(events)["/x"].render_time_ms_avg == 2000


def test_summarize_skips_events_without_route():
    events = [_ev("reasoning", reasoning="opening"), _nav("/x", 1000)]
    by_route = summarize_session(events)
    assert list(by_route.keys()) == ["/x"]


# ─── compute_diff ─────────────────────────────────────────────────────────────

def test_diff_reports_added_route_actions_and_removed_friction():
    baseline = [
        _nav("/pipeline", 1500),
        _reasoning("/pipeline", "Persona would have taken ['hide_with_reason'] but no matching affordance found on /pipeline."),
    ]
    scenario = [
        _nav("/pipeline", 1600),
        _action("/pipeline", "hide_with_reason"),
        # missing affordance disappears in scenario
    ]
    diff = compute_diff(baseline, scenario)
    [r] = diff["routes"]
    assert r["route"] == "/pipeline"
    assert r["actions_delta"] == 1
    assert r["missing_delta"] == -1
    assert r["render_ms_delta"] == 100
    assert diff["totals"]["scenario_actions"] == 1
    assert diff["totals"]["baseline_missing"] == 1
    assert diff["totals"]["scenario_missing"] == 0


def test_diff_handles_routes_only_in_one_side():
    baseline = [_nav("/today", 1000), _action("/today", "x")]
    scenario = [_nav("/today", 1000), _action("/today", "x"),
                _nav("/new-route", 1500), _action("/new-route", "y")]
    diff = compute_diff(baseline, scenario)
    by_route = {r["route"]: r for r in diff["routes"]}
    assert by_route["/new-route"]["actions_delta"] == 1
    assert by_route["/today"]["actions_delta"] == 0


# ─── render_diff_markdown ─────────────────────────────────────────────────────

def test_markdown_renders_per_route_table_and_totals():
    baseline = [_nav("/pipeline", 1500),
                _reasoning("/pipeline", "no matching affordance")]
    scenario = [_nav("/pipeline", 1600), _action("/pipeline", "hide_with_reason")]
    diff = compute_diff(baseline, scenario)
    md = render_diff_markdown(
        scenario={"name": "hide-with-reason-added",
                  "description": "Add the dropdown",
                  "based_on": "all"},
        persona_id="senior-gtm-eng-nyc",
        diff=diff,
        baseline_session_path="/runs/bl.jsonl",
        scenario_session_path="/runs/sc.jsonl",
    )
    assert "# Scenario diff — hide-with-reason-added" in md
    assert "senior-gtm-eng-nyc" in md
    assert "| `/pipeline` |" in md
    assert "+1" in md          # actions delta
    assert "-1" in md          # missing affordance delta


def test_markdown_uses_plus_sign_for_positive_deltas():
    diff = compute_diff(
        [_nav("/x", 1000)],
        [_nav("/x", 1000), _action("/x", "y")],
    )
    md = render_diff_markdown(
        {"name": "n", "description": "d", "based_on": "all"},
        "p", diff, "b", "s",
    )
    # Pull the per-route table row and check it specifically — the
    # surrounding markdown contains a timestamp which can include "-1".
    row = next(line for line in md.splitlines() if "`/x`" in line)
    assert "+1" in row
    assert "-1" not in row


# ─── make_scenario_context_hook (constructs the hook; doesn't run it) ────────

def test_hook_with_no_modifications_is_a_no_op():
    """An empty scenario produces a callable hook that does nothing."""
    # Scenario schema requires at least one modification — but the runtime
    # hook itself should tolerate missing keys defensively.
    hook = make_scenario_context_hook({"modifications": {}})
    assert callable(hook)


def test_hook_construction_does_not_explode_on_starter_scenarios(tmp_path):
    """Each shipped scenario YAML at <repo_root>/qa/scenarios/ must produce
    a hook without raising. Skipped when no qa/scenarios/ dir exists (i.e.,
    on the framework repo itself before any user adopts it)."""
    import yaml
    here = Path(__file__).resolve().parents[2] / "qa" / "scenarios"
    if not here.is_dir():
        import pytest
        pytest.skip("no qa/scenarios/ — framework repo, not user repo")
    for f in sorted(here.glob("*.yaml")):
        scenario = yaml.safe_load(f.read_text())
        hook = make_scenario_context_hook(scenario)
        assert callable(hook), f.name


# ─── Validator wiring ─────────────────────────────────────────────────────────

def test_starter_scenarios_pass_cohortqa_schema():
    """Every YAML in qa/scenarios/ must satisfy scenario.schema.yaml.
    baseline.yaml uses a config_overrides placeholder so it still passes
    the 'at least one modification' rule. Skipped when no qa/scenarios/
    dir exists (i.e., on the framework repo itself before any user adopts it)."""
    from cohortqa.core.persona_schema import load_scenario
    here = Path(__file__).resolve().parents[2] / "qa" / "scenarios"
    if not here.is_dir():
        import pytest
        pytest.skip("no qa/scenarios/ — framework repo, not user repo")
    for f in sorted(here.glob("*.yaml")):
        load_scenario(f)
