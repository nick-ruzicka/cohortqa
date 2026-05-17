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
