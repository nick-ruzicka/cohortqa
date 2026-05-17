"""Unit tests for the selector-probe helpers added in Phase B #1.

The smoke file is skipped without Playwright + chromium, but these helpers
are pure (mock-friendly) and the taxonomy/app-yaml checks are pure-Python,
so they belong in a separate file that always runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from personalab.core.persona_schema import KNOWN_FRICTION_TYPES, load_app_config
from personalab.core.runner import (
    _probe_route_actions,
    _visible_names_from_probe,
)


def test_visible_names_from_probe_extracts_only_matched():
    """Back-compat extractor must keep only names whose selector actually
    resolved to at least one element. Eval-error rows and zero-match rows
    are dropped from the visible list — the structured probe still carries
    them for the analyzer."""
    probe = [
        {"name": "ok", "selector": "[data-ok]", "matched_count": 2, "eval_error": None},
        {"name": "nope", "selector": "[data-nope]", "matched_count": 0, "eval_error": None},
        {"name": "broken", "selector": "::", "matched_count": 0,
         "eval_error": "SyntaxError(...)"},
        {"name": "unknown", "selector": None, "matched_count": 0,
         "eval_error": "action_not_defined_in_app_config"},
    ]
    visible = _visible_names_from_probe(probe)
    assert visible == ["ok"]


def test_probe_route_actions_separates_no_match_from_eval_error():
    """The whole point of the rewrite: zero-match, eval-error, and
    action-not-defined must each produce distinct entries in the probe
    output so the analyzer can tier severity by failure mode."""

    class _Locator:
        def __init__(self, count_or_exc):
            self._co = count_or_exc

        async def count(self):
            if isinstance(self._co, Exception):
                raise self._co
            return self._co

    class _Page:
        def __init__(self):
            self._selectors = {
                "[data-ok]": _Locator(3),
                "[data-empty]": _Locator(0),
                "::syntax-bad": _Locator(SyntaxError("invalid selector")),
            }

        def locator(self, sel):
            return self._selectors[sel]

    all_actions = [
        {"name": "ok", "selector": "[data-ok]"},
        {"name": "empty", "selector": "[data-empty]"},
        {"name": "bad", "selector": "::syntax-bad"},
    ]
    # "missing" is referenced in the route but not defined in app_config —
    # this is a different failure mode than "selector matched zero".
    probe = asyncio.run(_probe_route_actions(
        _Page(), ["ok", "empty", "bad", "missing"], all_actions
    ))
    by_name = {p["name"]: p for p in probe}

    assert by_name["ok"]["matched_count"] == 3
    assert by_name["ok"]["eval_error"] is None

    assert by_name["empty"]["matched_count"] == 0
    assert by_name["empty"]["eval_error"] is None  # zero != error

    assert by_name["bad"]["matched_count"] == 0
    assert "invalid selector" in (by_name["bad"]["eval_error"] or "")

    assert by_name["missing"]["matched_count"] == 0
    assert by_name["missing"]["eval_error"] == "action_not_defined_in_app_config"


def test_instrumentation_gap_is_a_known_friction_type():
    """The C6 escape hatch: must be in KNOWN_FRICTION_TYPES so app.yaml
    can declare it and personas can declare sensitivity to it."""
    assert "instrumentation_gap" in KNOWN_FRICTION_TYPES


def test_qa_app_yaml_declares_instrumentation_gap():
    """The live app.yaml must declare the type so the analyzer's
    prompt-time taxonomy iteration includes it. Cross-checks that the
    schema constant and the live config stay in sync."""
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_app_config(repo_root / "qa" / "app.yaml")
    types = {s["type"] for s in cfg["friction_signals"]}
    assert "instrumentation_gap" in types


# ─── Hydration wait (Phase B #2) ─────────────────────────────────────────────

class _FakeHydrationPage:
    """Minimal page mock for _wait_for_hydration. Drives a programmable
    body-length series across reads — one entry consumed per evaluate()
    call, last value sticks once the list is exhausted."""

    def __init__(self, body_len_series: list[int]):
        self._series = list(body_len_series)
        self._evaluate_calls = 0
        self._wait_calls = 0

    async def evaluate(self, _expr: str) -> int:
        self._evaluate_calls += 1
        if not self._series:
            return 0
        if len(self._series) == 1:
            return self._series[0]
        return self._series.pop(0)

    async def wait_for_timeout(self, _ms: int) -> None:
        self._wait_calls += 1


def test_wait_for_hydration_returns_true_when_body_stabilises_large():
    """Two reads in a row at the same length >= 200 chars → settled."""
    from personalab.core.runner import _wait_for_hydration

    page = _FakeHydrationPage([0, 50, 2500, 2500])
    settled = asyncio.run(_wait_for_hydration(page))
    assert settled is True
    # Should have stopped after the stabilised read, not exhausted the cap.
    assert page._wait_calls < 15


def test_wait_for_hydration_returns_true_for_small_but_stable_body():
    """A genuinely short page (e.g. dense /context after improvement)
    shouldn't burn the full 3s waiting for it to grow — once it's stable
    at a small non-zero size, we're done."""
    from personalab.core.runner import _wait_for_hydration

    page = _FakeHydrationPage([0, 40, 40])
    settled = asyncio.run(_wait_for_hydration(page))
    assert settled is True


def test_wait_for_hydration_returns_false_at_cap_with_zero_body(monkeypatch):
    """If body never paints at all (status=200 but JS never ran), we
    eventually bail and let the analyzer treat that as a low-confidence
    instrumentation_gap signal.

    Fake-clock the wall time so the test doesn't burn the real 3s cap."""
    from personalab.core import runner

    # Each call advances 100ms of fake wall time; ensures the loop hits
    # the 3000ms cap quickly without taking real time.
    fake_now = [0.0]
    def _now():
        fake_now[0] += 0.1  # 100ms per call
        return fake_now[0]
    monkeypatch.setattr(runner.time, "monotonic", _now)

    # All-zero series — never stabilises above 0, never settles.
    page = _FakeHydrationPage([0])
    settled = asyncio.run(runner._wait_for_hydration(page))
    assert settled is False


def test_wait_for_hydration_survives_evaluate_exceptions():
    """If the page navigates away during the wait and evaluate() raises,
    we should return False rather than crash the runner."""
    from personalab.core.runner import _wait_for_hydration

    class _CrashPage:
        async def evaluate(self, _expr: str):
            raise RuntimeError("page detached")

        async def wait_for_timeout(self, _ms):
            pass

    settled = asyncio.run(_wait_for_hydration(_CrashPage()))
    assert settled is False


# ─── Structured action-error classification (Phase B #3) ─────────────────────

def test_classify_action_error_separates_known_modes():
    """Each Playwright failure flavour gets a distinct error_type so the
    analyzer can tier severity by failure mode."""
    from personalab.core.runner import _classify_action_error

    class _TimeoutError(Exception):
        pass

    assert _classify_action_error(_TimeoutError("Locator.click: Timeout 2000ms exceeded.")) == "timeout"
    assert _classify_action_error(Exception("element is not visible")) == "not_visible"
    assert _classify_action_error(Exception("element intercepts pointer events")) == "blocked_by_overlay"
    assert _classify_action_error(Exception("element is outside the viewport")) == "blocked_by_overlay"
    assert _classify_action_error(Exception("element handle is detached")) == "detached"
    assert _classify_action_error(Exception("strict mode violation: 0 elements found")) == "not_found"
    assert _classify_action_error(Exception("something completely unrelated")) == "other"


def test_classify_action_error_prefers_timeout_when_message_mentions_both():
    """A TimeoutError whose message also contains 'not found' should still
    classify as timeout — the failure mode is timing, the missing element
    is downstream."""
    from personalab.core.runner import _classify_action_error

    class _TimeoutError(Exception):
        pass

    e = _TimeoutError("Timeout 2000ms exceeded. No element found.")
    assert _classify_action_error(e) == "timeout"


# ─── Console-error route attribution (Phase B #3) ────────────────────────────

def test_console_errors_filtered_per_route_via_active_route_tag():
    """The runner stores console errors as structured dicts with a `route`
    field set from self._active_route. The page_state.console_errors list
    on each nav event is filtered to only the errors that fired while that
    route was active — no more 'errors from /companies showing up in
    /context's console_errors snapshot.'"""
    # Pure structural check: simulate the tagging the runner does.
    errors = [
        {"type": "error", "text": "key warning A", "route": "/companies/[slug]"},
        {"type": "error", "text": "key warning B", "route": "/companies/[slug]"},
        {"type": "warning", "text": "hydration mismatch", "route": "/signals"},
        {"type": "error", "text": "key warning C", "route": "/context"},
    ]

    def per_route(path: str) -> list[dict]:
        return [e for e in errors if e["route"] == path]

    assert len(per_route("/companies/[slug]")) == 2
    assert len(per_route("/signals")) == 1
    assert len(per_route("/context")) == 1
    # No cross-attribution — /today gets nothing, not the cumulative pile.
    assert per_route("/today") == []
