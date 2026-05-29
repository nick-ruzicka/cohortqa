"""Phase C regression tests.

Phase C fixed two architectural quirks the Phase B audit flagged as
dormant:

1. action_index reset per-route → never reached the back-button-sim
   threshold for personas with 1-2 actions per route.
   Fix: session-wide ``self._session_action_index`` on PersonaRunner.

2. trust_filters_action ran INSIDE the dispatch loop, after
   chooses_action had already excluded actions with trust-relevant
   side_effects but the wrong behavioral category. Paranoid posture
   was invisible in the trace.
   Fix: pre-pass at the top of _take_route_actions walks ALL declared
   route actions and records refusals before chooses_action filters.

These tests use a lightweight async stub page rather than real
Playwright so they run in the same ~10s as the rest of the suite.
The end-to-end smoke coverage lives in test_runner_smoke.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cohortqa.core.runner import PersonaRunner


# ─── Lightweight async stubs ──────────────────────────────────────────────────


class _StubLocator:
    """Resolves click/focus/tap/dblclick without doing anything. Records
    each call so tests can assert on it."""

    def __init__(self, ledger: list[tuple[str, Any]]) -> None:
        self._ledger = ledger
        self.first = self

    async def click(self, **_: Any) -> None:
        self._ledger.append(("click", None))

    async def focus(self, **_: Any) -> None:
        self._ledger.append(("focus", None))

    async def tap(self, **_: Any) -> None:
        self._ledger.append(("tap", None))


class _StubKeyboard:
    def __init__(self, ledger: list[tuple[str, Any]]) -> None:
        self._ledger = ledger

    async def press(self, key: str) -> None:
        self._ledger.append(("keyboard.press", key))


class _StubPage:
    """Just enough async surface for _take_route_actions to run without
    Playwright. Records click/focus/tap calls in a ledger the tests
    can inspect."""

    def __init__(self) -> None:
        self.ledger: list[tuple[str, Any]] = []
        self.keyboard = _StubKeyboard(self.ledger)

    def locator(self, selector: str) -> _StubLocator:
        self.ledger.append(("locator", selector))
        return _StubLocator(self.ledger)

    async def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
        return None

    async def go_back(self, **_: Any) -> None:
        self.ledger.append(("go_back", None))

    async def expect_navigation(self, **_: Any):
        class _NoopCtx:
            async def __aenter__(self):  # noqa: D401
                return self
            async def __aexit__(self, *exc):  # noqa: D401
                return False
        return _NoopCtx()


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _persona_paranoid() -> dict:
    return {
        "identity": {"name": "p", "role": "p", "background": "p"},
        "target_archetypes": [],
        "location_preferences": [],
        "comp_floor": 0,
        "behavioral": {
            "click_speed": "fast",
            "reads_details": False,
            "rejection_threshold": "medium",
            "detail_dwell_ms": 0,
            "trust_posture": "paranoid",
        },
        "meta_attitude": "paranoid",
        "friction_sensitivities": [],
    }


def _persona_trusting() -> dict:
    p = _persona_paranoid()
    p["behavioral"]["trust_posture"] = "trusting"
    return p


def _persona_lost_high_error() -> dict:
    return {
        "identity": {"name": "p", "role": "p", "background": "p"},
        "target_archetypes": [],
        "location_preferences": [],
        "comp_floor": 0,
        "behavioral": {
            "click_speed": "fast",
            "reads_details": False,
            "rejection_threshold": "medium",
            "detail_dwell_ms": 0,
            "goal_clarity": "lost",
            "error_rate": "high",
        },
        "meta_attitude": "x",
        "friction_sensitivities": [],
    }


def _app_with_persists_action() -> dict:
    """App where the persists:-tagged action has category=`other` and
    would NEVER be chosen by ``chooses_action``. The whole point of
    Phase C Fix 2 is that paranoid personas still record refusal for
    these."""
    return {
        "app": {"name": "x", "dev_server": "http://localhost:0",
                "description": "test"},
        "routes": [{
            "path": "/r",
            "purpose": "test",
            "actions": ["run_discovery", "open_detail"],
            "expected_load_time_ms": 5000,
        }],
        "actions": [
            {
                # ``run_discovery`` is category=`other` (chooses_action excludes
                # it for every persona). Has persists: → paranoid refuses.
                "name": "run_discovery",
                "selector": 'button.run',
                "side_effects": ["persists:discovery_results"],
            },
            {
                "name": "open_detail",
                "selector": 'a.detail',
                "side_effects": [],
            },
        ],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
        ],
        "personas_dir": "personas",
        "scenarios_dir": "scenarios",
        "runs_dir": "runs",
    }


def _app_two_routes() -> dict:
    """Two-route app for the session-counter test. Each route declares
    one chooseable nav action so the dispatch loop runs once per route
    and the counter has somewhere to advance."""
    return {
        "app": {"name": "x", "dev_server": "http://localhost:0",
                "description": "test"},
        "routes": [
            {
                "path": "/r1",
                "purpose": "first",
                "actions": ["view_one"],
                "expected_load_time_ms": 5000,
            },
            {
                "path": "/r2",
                "purpose": "second",
                "actions": ["view_two"],
                "expected_load_time_ms": 5000,
            },
        ],
        "actions": [
            {"name": "view_one", "selector": 'a.one'},
            {"name": "view_two", "selector": 'a.two'},
        ],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
        ],
        "personas_dir": "p", "scenarios_dir": "s", "runs_dir": "r",
    }


def _make_runner(persona: dict, app: dict, tmp_path: Path) -> PersonaRunner:
    return PersonaRunner(
        persona=persona,
        persona_id="test",
        app_config=app,
        runs_dir=tmp_path,
    )


# ─── Fix 2 — Trust pre-pass tests ─────────────────────────────────────────────


def test_trust_pre_pass_records_refusal_for_chooses_action_excluded_action(tmp_path):
    """The persists:-tagged action has category=`other` and is filtered
    out by chooses_action. Phase C requires the trust filter to still
    record a refusal so paranoid posture is visible in the trace.
    """
    runner = _make_runner(
        _persona_paranoid(), _app_with_persists_action(), tmp_path,
    )
    page = _StubPage()
    route = runner.app_config["routes"][0]
    visible = ["run_discovery", "open_detail"]

    asyncio.run(runner._take_route_actions(page, route, visible))

    refusals = [
        e for e in runner.events
        if e.event_type == "reasoning"
        and e.action == "run_discovery"
        and "trust" in (e.reasoning or "")
        and "refuses" in (e.reasoning or "")
    ]
    assert len(refusals) == 1, (
        "expected exactly one trust refusal for run_discovery; "
        f"got events: {[(e.event_type, e.action, e.reasoning) for e in runner.events]}"
    )
    assert "Phase C pre-pass" in refusals[0].reasoning


def test_trust_pre_pass_silent_for_trusting_persona(tmp_path):
    """A trusting persona on the same route generates ZERO trust
    refusal events — the pre-pass is gated on trust_posture != trusting.
    """
    runner = _make_runner(
        _persona_trusting(), _app_with_persists_action(), tmp_path,
    )
    page = _StubPage()
    route = runner.app_config["routes"][0]
    visible = ["run_discovery", "open_detail"]

    asyncio.run(runner._take_route_actions(page, route, visible))

    refusals = [
        e for e in runner.events
        if e.reasoning and "trust" in e.reasoning and "refuses" in e.reasoning
    ]
    assert refusals == [], (
        f"trusting persona produced trust refusals: {[e.reasoning for e in refusals]}"
    )


def test_trust_pre_pass_does_not_double_record(tmp_path):
    """A paranoid persona facing a chooses_action-approved action with
    trust-relevant side_effects: pre-pass records ONE refusal, dispatch
    loop should NOT also record a duplicate (the old Phase B inline
    trust check is gone in Phase C).
    """
    # Build an app where the trust-relevant action IS chooses_action-approved.
    # ``view_settings`` has category=`navigation` (starts with `view_`) so
    # chooses_action returns True for everyone. The persists: side-effect
    # then makes paranoid refuse.
    app = {
        "app": {"name": "x", "dev_server": "http://localhost:0",
                "description": "test"},
        "routes": [{
            "path": "/r",
            "purpose": "t",
            "actions": ["view_settings"],
            "expected_load_time_ms": 5000,
        }],
        "actions": [{
            "name": "view_settings",
            "selector": 'a.s',
            "side_effects": ["persists:profile"],
        }],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
        ],
        "personas_dir": "p", "scenarios_dir": "s", "runs_dir": "r",
    }
    runner = _make_runner(_persona_paranoid(), app, tmp_path)
    page = _StubPage()

    asyncio.run(runner._take_route_actions(
        page, runner.app_config["routes"][0], ["view_settings"],
    ))

    refusals = [
        e for e in runner.events
        if e.reasoning and "trust" in e.reasoning and "refuses" in e.reasoning
    ]
    assert len(refusals) == 1, (
        f"expected exactly one refusal; got {len(refusals)}: "
        f"{[e.reasoning for e in refusals]}"
    )
    # Pre-pass owns the recording — its signature is in the reasoning.
    assert "Phase C pre-pass" in refusals[0].reasoning


def test_trust_pre_pass_skips_dispatch_for_refused_actions(tmp_path):
    """After the pre-pass records a refusal, the dispatch loop must NOT
    click the action — refused_by_trust filters it out before clicks.
    """
    app = {
        "app": {"name": "x", "dev_server": "http://localhost:0",
                "description": "test"},
        "routes": [{
            "path": "/r",
            "purpose": "t",
            "actions": ["view_settings"],
            "expected_load_time_ms": 5000,
        }],
        "actions": [{
            "name": "view_settings",
            "selector": 'a.s',
            "side_effects": ["persists:profile"],
        }],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
        ],
        "personas_dir": "p", "scenarios_dir": "s", "runs_dir": "r",
    }
    runner = _make_runner(_persona_paranoid(), app, tmp_path)
    page = _StubPage()

    asyncio.run(runner._take_route_actions(
        page, runner.app_config["routes"][0], ["view_settings"],
    ))

    # No click/focus/tap should have been dispatched.
    click_calls = [c for c in page.ledger if c[0] in ("click", "focus", "tap")]
    assert click_calls == [], (
        f"refused action was still dispatched: {click_calls}"
    )
    # And no action event in the trace.
    action_events = [e for e in runner.events if e.event_type == "action"]
    assert action_events == [], (
        f"refused action created an action event: "
        f"{[(e.event_type, e.action) for e in action_events]}"
    )


# ─── Fix 1 — Session-wide action_index tests ──────────────────────────────────


def test_session_action_index_starts_at_zero(tmp_path):
    """Freshly-constructed runner has _session_action_index = 0."""
    runner = _make_runner(_persona_trusting(), _app_two_routes(), tmp_path)
    assert runner._session_action_index == 0


def test_session_action_index_advances_across_routes(tmp_path):
    """After dispatching one action per route across two routes, the
    counter should be at 2 — proving it doesn't reset per route.
    """
    runner = _make_runner(
        _persona_trusting(), _app_two_routes(), tmp_path,
    )
    page = _StubPage()

    asyncio.run(runner._take_route_actions(
        page, runner.app_config["routes"][0], ["view_one"],
    ))
    after_route_1 = runner._session_action_index

    asyncio.run(runner._take_route_actions(
        page, runner.app_config["routes"][1], ["view_two"],
    ))
    after_route_2 = runner._session_action_index

    assert after_route_1 == 1, (
        f"counter should be 1 after one dispatched action; got {after_route_1}"
    )
    assert after_route_2 == 2, (
        f"counter should be 2 after two dispatched actions across two routes "
        f"(Phase C: session-wide, not per-route); got {after_route_2}"
    )


def test_session_action_index_only_advances_on_dispatched_actions(tmp_path):
    """Protected actions and trust-refused actions skip the click via
    `continue` / pre-pass filtering. Those skips must NOT advance the
    counter — otherwise replay determinism breaks on declared-vs-
    dispatched-action drift between runs.
    """
    # Two declared actions, only one dispatched (the other is protected).
    app = {
        "app": {"name": "x", "dev_server": "http://localhost:0",
                "description": "test"},
        "routes": [{
            "path": "/r",
            "purpose": "t",
            "actions": ["protected_one", "view_two"],
            "expected_load_time_ms": 5000,
        }],
        "actions": [
            {
                "name": "protected_one",
                "selector": 'button.p',
                "side_effects": ["writes:applications.md"],
            },
            {"name": "view_two", "selector": 'a.t'},
        ],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
        ],
        "personas_dir": "p", "scenarios_dir": "s", "runs_dir": "r",
    }
    runner = _make_runner(_persona_trusting(), app, tmp_path)
    page = _StubPage()

    asyncio.run(runner._take_route_actions(
        page, runner.app_config["routes"][0],
        ["protected_one", "view_two"],
    ))

    # protected_one was suppressed; view_two dispatched. Counter = 1.
    assert runner._session_action_index == 1, (
        f"counter should reflect only dispatched actions; got "
        f"{runner._session_action_index}"
    )
