"""Tests for the session replayer.

All pure-logic tests run without Playwright. A single end-to-end smoke
test uses Playwright route interception to replay a synthetic session
against synthetic pages — skipped automatically if Playwright is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cohortqa.core.replayer import (
    RENDER_TOLERANCE_FLOOR_MS,
    ReplayStep,
    StepResult,
    build_replay_plan,
    compute_replay_diff,
    render_replay_markdown,
)


def _app_config() -> dict:
    return {
        "app": {"name": "T", "dev_server": "http://t", "description": "."},
        "routes": [{"path": "/x", "purpose": ".", "actions": ["click_x"]}],
        "actions": [
            {"name": "click_x", "selector": "[data-x]"},
            {"name": "click_y", "selector": "[data-y]"},
        ],
        "friction_signals": [{"type": "navigation", "description": "."}],
        "personas_dir": "p",
        "scenarios_dir": "s",
        "runs_dir": "r",
    }


# ─── build_replay_plan ────────────────────────────────────────────────────────

def test_build_plan_groups_actions_under_their_nav():
    events = [
        {"event_type": "reasoning", "reasoning": "opening"},
        {"event_type": "nav", "route": "/x", "render_time_ms": 1500,
         "page_state": {"visible_action_names": ["click_x"], "console_errors": []}},
        {"event_type": "action", "route": "/x", "action": "click_x", "selector": "[data-x]"},
        {"event_type": "nav", "route": "/y", "render_time_ms": 800,
         "page_state": {"visible_action_names": ["click_y"], "console_errors": []}},
        {"event_type": "action", "route": "/y", "action": "click_y", "selector": "[data-y]"},
    ]
    plan = build_replay_plan(events, _app_config())
    assert [s.route for s in plan] == ["/x", "/y"]
    assert plan[0].actions_to_take == [{"name": "click_x", "selector": "[data-x]"}]
    assert plan[0].expected_render_ms == 1500
    assert plan[0].expected_visible_actions == ["click_x"]


def test_build_plan_folds_detail_route_navs_into_parent():
    events = [
        {"event_type": "nav", "route": "/companies", "render_time_ms": 1000,
         "page_state": {"visible_action_names": [], "console_errors": []}},
        {"event_type": "action", "route": "/companies", "action": "open_company_detail"},
        # Detail route — should NOT become its own step.
        {"event_type": "nav", "route": "/companies/[slug]", "render_time_ms": 0,
         "page_state": {
             "visible_action_names": [], "console_errors": [],
             "entered_via": "detail_route_traversal",
         }},
    ]
    plan = build_replay_plan(events, _app_config())
    assert [s.route for s in plan] == ["/companies"]


def test_build_plan_pulls_selector_from_app_config_when_action_event_omits_it():
    events = [
        {"event_type": "nav", "route": "/x", "render_time_ms": 100,
         "page_state": {"visible_action_names": [], "console_errors": []}},
        {"event_type": "action", "route": "/x", "action": "click_x"},  # no selector
    ]
    plan = build_replay_plan(events, _app_config())
    assert plan[0].actions_to_take[0]["selector"] == "[data-x]"


def test_build_plan_falls_back_to_event_selector_when_app_config_missing():
    events = [
        {"event_type": "nav", "route": "/x", "render_time_ms": 100,
         "page_state": {"visible_action_names": [], "console_errors": []}},
        {"event_type": "action", "route": "/x", "action": "unknown_action",
         "selector": "[data-from-event]"},
    ]
    plan = build_replay_plan(events, _app_config())
    assert plan[0].actions_to_take[0]["selector"] == "[data-from-event]"


# ─── compute_replay_diff ─────────────────────────────────────────────────────

def _step(route: str, render_ms: int, visible: list[str], actions=None) -> ReplayStep:
    return ReplayStep(
        route=route, expected_render_ms=render_ms,
        expected_visible_actions=visible,
        expected_console_errors=0,
        actions_to_take=actions or [],
    )


def _obs(route: str, render_ms: int, visible: list[str],
         console_errors: int = 0, nav_ok: bool = True,
         action_results=None) -> StepResult:
    return StepResult(
        route=route, nav_succeeded=nav_ok,
        render_ms=render_ms, visible_actions=visible,
        console_errors=console_errors, nav_error=None,
        action_results=action_results or [],
    )


def test_diff_clean_run_is_ok():
    plan = [_step("/x", 1000, ["click_x"])]
    obs = [_obs("/x", 1050, ["click_x"])]
    diff = compute_replay_diff(plan, obs)
    assert diff["summary"]["ok"] is True
    assert diff["summary"]["render_regressions"] == 0
    assert diff["steps"][0]["render_regression"] is False


def test_diff_flags_render_regression_outside_tolerance():
    # Expected 1000ms, actual 2000ms → 100% over (above 50% tolerance and
    # above the 100ms floor).
    plan = [_step("/x", 1000, [])]
    obs = [_obs("/x", 2000, [])]
    diff = compute_replay_diff(plan, obs)
    assert diff["steps"][0]["render_regression"] is True
    assert diff["summary"]["render_regressions"] == 1
    assert diff["summary"]["ok"] is False


def test_diff_ignores_render_change_within_tolerance():
    # 1000ms → 1400ms = 40% slower (within 50% band).
    plan = [_step("/x", 1000, [])]
    obs = [_obs("/x", 1400, [])]
    diff = compute_replay_diff(plan, obs)
    assert diff["steps"][0]["render_regression"] is False


def test_diff_ignores_small_absolute_render_change_even_with_high_pct():
    # 50ms → 90ms is 80% slower but only 40ms — below the floor.
    plan = [_step("/x", 50, [])]
    obs = [_obs("/x", 90, [])]
    assert (90 - 50) <= RENDER_TOLERANCE_FLOOR_MS
    diff = compute_replay_diff(plan, obs)
    assert diff["steps"][0]["render_regression"] is False


def test_diff_flags_missing_action_as_failure():
    plan = [_step("/x", 1000, ["click_x", "click_y"])]
    obs = [_obs("/x", 1000, ["click_x"])]  # click_y disappeared
    diff = compute_replay_diff(plan, obs)
    assert diff["steps"][0]["missing_actions"] == ["click_y"]
    assert diff["summary"]["ok"] is False


def test_diff_treats_new_actions_as_additive_not_failure():
    plan = [_step("/x", 1000, ["click_x"])]
    obs = [_obs("/x", 1000, ["click_x", "click_y"])]  # added one
    diff = compute_replay_diff(plan, obs)
    assert diff["steps"][0]["new_actions"] == ["click_y"]
    assert diff["summary"]["ok"] is True


def test_diff_flags_action_failure():
    plan = [_step("/x", 1000, ["click_x"], actions=[{"name": "click_x", "selector": "[data-x]"}])]
    obs = [_obs("/x", 1000, ["click_x"],
                action_results=[{"name": "click_x", "ok": False, "error": "timeout"}])]
    diff = compute_replay_diff(plan, obs)
    assert diff["summary"]["action_failures"] == 1
    assert diff["summary"]["ok"] is False


def test_diff_flags_nav_failure():
    plan = [_step("/x", 1000, [])]
    obs = [StepResult(route="/x", nav_succeeded=False, render_ms=10000,
                      visible_actions=[], console_errors=0,
                      nav_error="goto timeout")]
    diff = compute_replay_diff(plan, obs)
    assert diff["summary"]["nav_failures"] == 1
    assert diff["summary"]["ok"] is False


def test_diff_counts_new_console_errors():
    plan = [_step("/x", 1000, [])]
    plan[0].expected_console_errors = 1  # had 1 in baseline
    obs = [_obs("/x", 1000, [], console_errors=3)]  # now 3
    diff = compute_replay_diff(plan, obs)
    assert diff["steps"][0]["new_console_errors"] == 2
    assert diff["summary"]["new_console_errors"] == 2
    assert diff["summary"]["ok"] is False


# ─── Markdown rendering ──────────────────────────────────────────────────────

def test_markdown_marks_clean_replay_with_ok_status():
    plan = [_step("/x", 1000, ["click_x"])]
    obs = [_obs("/x", 1100, ["click_x"])]
    diff = compute_replay_diff(plan, obs)
    md = render_replay_markdown(Path("sess.jsonl"), plan, obs, diff)
    assert "✅" in md
    assert "Steps replayed: 1" in md
    # Clean step shows render line, no regression alarm.
    assert "render 1100ms vs expected 1000ms" in md
    assert "🚨" not in md


def test_markdown_marks_regression_with_alarm_status():
    plan = [_step("/x", 1000, ["click_x"])]
    obs = [_obs("/x", 5000, [])]  # huge regression + missing action
    diff = compute_replay_diff(plan, obs)
    md = render_replay_markdown(Path("sess.jsonl"), plan, obs, diff)
    assert "🚨" in md
    assert "missing: ['click_x']" in md


# ─── End-to-end via Playwright route interception ────────────────────────────

playwright = pytest.importorskip("playwright.async_api")


def test_replay_smoke_runs_against_route_interception(tmp_path):
    """Write a synthetic session JSONL, then replay it against a Playwright
    BrowserContext whose routes are all fulfilled with synthetic HTML.
    The replay should succeed with no regressions."""
    import asyncio

    from cohortqa.core.replayer import SessionReplayer

    # 1. Write a synthetic past session: visited /x, took click_x.
    session_path = tmp_path / "past-session.jsonl"
    session_events = [
        {"ts": "t", "persona_id": "p", "source": "cohortqa:p",
         "event_type": "reasoning", "reasoning": "opening"},
        {"ts": "t", "persona_id": "p", "source": "cohortqa:p",
         "event_type": "nav", "route": "/x", "render_time_ms": 200,
         "page_state": {"url": "http://test.local/x", "status": 200,
                        "title": "X", "body_text_length": 100,
                        "visible_action_names": ["click_x"],
                        "console_errors": [], "nav_error": None}},
        {"ts": "t", "persona_id": "p", "source": "cohortqa:p",
         "event_type": "action", "route": "/x", "action": "click_x",
         "selector": '[data-x]'},
    ]
    session_path.write_text(
        "\n".join(json.dumps(e) for e in session_events) + "\n",
        encoding="utf-8",
    )

    # 2. Intercept all routes; serve a page that has the [data-x] element.
    async def intercept(context):
        async def handler(route, request):
            await route.fulfill(
                status=200, content_type="text/html",
                body="""<!doctype html><html><body>
                       <h1>X</h1>
                       <button data-x>Click X</button>
                       </body></html>""",
            )
        await context.route("**/*", handler)

    cfg = _app_config()
    cfg["app"]["dev_server"] = "http://test.local"

    replayer = SessionReplayer(
        app_config=cfg,
        original_session_path=session_path,
        regressions_dir=tmp_path / "regressions",
        headless=True,
        context_hook=intercept,
    )
    result = asyncio.run(replayer.replay())

    assert result["steps_replayed"] == 1
    assert result["diff"]["summary"]["ok"] is True, result["diff"]
    assert Path(result["report_path"]).exists()
    md = Path(result["report_path"]).read_text()
    assert "✅" in md
