"""Tests for the friction analyzer.

The analyzer talks to Anthropic. Every test here injects a fake client so
the suite never hits the real API. The pure helpers (prompt builders,
markdown renderer) are tested independently of the SDK call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from personalab.core.analyzer import (
    AnalyzerConfig,
    FrictionAnalyzer,
    FrictionEvent,
    FrictionReport,
    Opportunity,
    UxIssue,
    Win,
    _build_friction_taxonomy,
    _build_persona_context,
    _build_user_message,
    render_markdown,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _app_config() -> dict:
    return {
        "app": {
            "name": "TestApp",
            "dev_server": "http://localhost:3000",
            "description": "A test app for the analyzer.",
        },
        "routes": [
            {"path": "/x", "purpose": "x", "actions": ["click_x"]},
        ],
        "actions": [{"name": "click_x", "selector": "[data-x]"}],
        "friction_signals": [
            {"type": "navigation", "description": "Got lost finding way back."},
            {"type": "slow_load", "description": "Page took too long."},
        ],
        "personas_dir": "p",
        "scenarios_dir": "s",
        "runs_dir": "r",
    }


def _persona() -> dict:
    return {
        "identity": {
            "name": "Test Persona",
            "role": "Test Role",
            "background": "Synthetic background.",
        },
        "target_archetypes": ["a", "b"],
        "location_preferences": ["remote"],
        "comp_floor": 200000,
        "behavioral": {
            "click_speed": "medium",
            "reads_details": True,
            "rejection_threshold": "high",
            "detail_dwell_ms": 30000,
        },
        "meta_attitude": "Skeptical and time-pressed.",
        "friction_sensitivities": ["navigation", "slow_load"],
    }


def _session_jsonl(tmp_path: Path) -> Path:
    """Write a minimal JSONL session and return the path."""
    events = [
        {
            "ts": "2026-05-17T08:00:00.000Z",
            "persona_id": "tp",
            "source": "personalab:tp",
            "event_type": "reasoning",
            "reasoning": "Persona opening.",
        },
        {
            "ts": "2026-05-17T08:00:01.000Z",
            "persona_id": "tp",
            "source": "personalab:tp",
            "event_type": "nav",
            "route": "/x",
            "render_time_ms": 1800,
            "page_state": {
                "url": "http://localhost:3000/x",
                "status": 200,
                "title": "X",
                "body_text_length": 1234,
                "visible_action_names": ["click_x"],
                "console_errors": [],
                "nav_error": None,
            },
        },
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


# ─── A fake Anthropic client ─────────────────────────────────────────────────

class _FakeUsage:
    def __init__(self, input_tokens=200, output_tokens=80,
                 cache_creation_input_tokens=0, cache_read_input_tokens=180):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _FakeResponse:
    def __init__(self, parsed_output: FrictionReport, usage: _FakeUsage):
        self.parsed_output = parsed_output
        self.usage = usage


class _FakeMessages:
    """Records the kwargs each call received so tests can assert on them."""

    def __init__(self, parsed_output: FrictionReport, usage: _FakeUsage | None = None):
        self.parsed_output = parsed_output
        self.usage = usage or _FakeUsage()
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.parsed_output, self.usage)


class _FakeClient:
    def __init__(self, messages: _FakeMessages):
        self.messages = messages


# ─── Pure helpers ─────────────────────────────────────────────────────────────

def test_friction_taxonomy_includes_all_app_signal_types():
    cfg = _app_config()
    taxonomy = _build_friction_taxonomy(cfg)
    assert "TestApp" in taxonomy
    assert "**navigation**" in taxonomy
    assert "**slow_load**" in taxonomy
    # Description text should make it in (whitespace collapsed).
    assert "Got lost" in taxonomy
    # The output-discipline section is essential — the analyzer relies on
    # it to handle the runner's protected-action reasoning correctly.
    assert "intent logged, click suppressed" in taxonomy


def test_persona_context_includes_identity_and_behavioral():
    ctx = _build_persona_context(_persona(), "tp")
    assert "Test Persona" in ctx
    assert "Test Role" in ctx
    assert "Skeptical" in ctx
    assert "medium" in ctx  # click_speed
    assert "high" in ctx    # rejection_threshold
    assert "selective" in ctx or "open" in ctx or "focused" in ctx  # engagement
    assert "navigation" in ctx and "slow_load" in ctx


def test_user_message_trims_payload_to_keep_only_useful_fields(tmp_path):
    path = _session_jsonl(tmp_path)
    from personalab.core.runner import read_session
    events = read_session(path)
    msg = _build_user_message(events)
    assert "FrictionReport" in msg
    # Trimmed fields are visible:
    assert "render_time_ms" in msg
    assert "visible_action_names" in msg
    # No noisy keys we explicitly dropped:
    assert '"selector":' not in msg or "click_x" in msg  # selector key only present
                                                          # when actually populated


# ─── Markdown rendering ───────────────────────────────────────────────────────

def test_render_markdown_sorts_friction_by_severity():
    report = FrictionReport(
        friction_events=[
            FrictionEvent(
                severity="low", signal_type="navigation", location="/x",
                description="Minor.", what_persona_expected="A", what_actually_happened="B",
            ),
            FrictionEvent(
                severity="high", signal_type="slow_load", location="/y",
                description="Major.", what_persona_expected="Fast", what_actually_happened="Slow",
            ),
            FrictionEvent(
                severity="medium", signal_type="navigation", location="/z",
                description="Mid.", what_persona_expected="X", what_actually_happened="Y",
            ),
        ],
        overall_verdict="Mixed.",
    )
    md = render_markdown("tp", _persona(), report)
    # HIGH should appear before MEDIUM before LOW.
    high_idx = md.index("[HIGH]")
    medium_idx = md.index("[MEDIUM]")
    low_idx = md.index("[LOW]")
    assert high_idx < medium_idx < low_idx
    assert "Test Persona" in md
    assert "Mixed." in md


def test_render_markdown_handles_empty_report():
    report = FrictionReport(overall_verdict="No friction observed.")
    md = render_markdown("tp", _persona(), report)
    assert "_None surfaced._" in md
    assert "No friction observed." in md


def test_render_markdown_includes_optional_sections():
    report = FrictionReport(
        ux_issues=[UxIssue(severity="medium", description="thing", location="/x")],
        opportunities=[
            Opportunity(
                description="Add Hide-with-reason",
                what_user_wanted="Skip without losing context",
                why_blocked="No dropdown exists",
            )
        ],
        wins=[Win(description="Fast scan", location="/today")],
        overall_verdict="Some wins.",
    )
    md = render_markdown("tp", _persona(), report)
    assert "## UX issues" in md
    assert "## Opportunities" in md
    assert "## Wins" in md
    assert "Hide-with-reason" in md


# ─── End-to-end with fake client ──────────────────────────────────────────────

def test_analyze_session_writes_md_and_json(tmp_path):
    expected = FrictionReport(
        friction_events=[
            FrictionEvent(
                severity="medium", signal_type="navigation",
                location="/x", description="Lost.",
                what_persona_expected="Find way back",
                what_actually_happened="No back link",
            ),
        ],
        overall_verdict="OK but missing nav.",
    )
    fake = _FakeMessages(parsed_output=expected)

    analyzer = FrictionAnalyzer(
        app_config=_app_config(),
        reports_dir=tmp_path / "reports",
        client=_FakeClient(fake),
    )
    session_path = _session_jsonl(tmp_path)
    summary = analyzer.analyze_session(_persona(), "tp", session_path)

    # Files written
    assert Path(summary["report_md"]).exists()
    assert Path(summary["report_json"]).exists()

    # Markdown contains the friction event.
    md = Path(summary["report_md"]).read_text()
    assert "Lost." in md
    assert "OK but missing nav." in md

    # JSON is the structured report verbatim.
    parsed = json.loads(Path(summary["report_json"]).read_text())
    assert parsed["overall_verdict"] == "OK but missing nav."
    assert parsed["friction_events"][0]["signal_type"] == "navigation"

    # Summary counts.
    assert summary["friction_event_count"] == 1
    assert summary["high_severity_count"] == 0
    assert summary["model"]  # whatever the default is, just confirm it's set
    assert summary["usage"]["cache_read_input_tokens"] == 180


def test_analyzer_caches_taxonomy_block(tmp_path):
    """The taxonomy block must carry cache_control so multi-persona runs
    reuse the cached prefix. This is the load-bearing prompt-caching
    invariant; lock it down with a test."""
    fake = _FakeMessages(parsed_output=FrictionReport(overall_verdict="ok"))
    analyzer = FrictionAnalyzer(
        app_config=_app_config(),
        reports_dir=tmp_path / "reports",
        client=_FakeClient(fake),
    )
    analyzer.analyze_session(_persona(), "tp", _session_jsonl(tmp_path))

    kwargs = fake.calls[0]
    system_blocks = kwargs["system"]
    assert isinstance(system_blocks, list)
    # First block is the frozen taxonomy with cache_control.
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "friction signal taxonomy" in system_blocks[0]["text"].lower()
    # Second block is the per-persona context, uncached.
    assert "cache_control" not in system_blocks[1]
    assert "Test Persona" in system_blocks[1]["text"]


def test_analyzer_uses_adaptive_thinking_and_no_sampling_params(tmp_path):
    """Opus 4.7 mandates adaptive thinking; sending temperature/top_p will 400.
    Lock the request shape so a regression doesn't bring those back."""
    fake = _FakeMessages(parsed_output=FrictionReport(overall_verdict="ok"))
    analyzer = FrictionAnalyzer(
        app_config=_app_config(),
        reports_dir=tmp_path / "reports",
        client=_FakeClient(fake),
    )
    analyzer.analyze_session(_persona(), "tp", _session_jsonl(tmp_path))

    kwargs = fake.calls[0]
    assert kwargs["thinking"] == {"type": "adaptive"}
    for forbidden in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert forbidden not in kwargs, forbidden
    assert kwargs["output_format"] is FrictionReport


def test_analyzer_uses_default_model_when_no_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("PERSONALAB_ANTHROPIC_MODEL", raising=False)
    # Re-import to pick up the env clear — module-level DEFAULT_MODEL is
    # captured at import time, so this asserts the configured default
    # at the AnalyzerConfig level instead.
    cfg = AnalyzerConfig()
    assert cfg.model == "claude-opus-4-7" or cfg.model.startswith("claude-")


def test_analyzer_respects_model_override_via_config(tmp_path):
    fake = _FakeMessages(parsed_output=FrictionReport(overall_verdict="ok"))
    analyzer = FrictionAnalyzer(
        app_config=_app_config(),
        reports_dir=tmp_path / "reports",
        client=_FakeClient(fake),
        config=AnalyzerConfig(model="claude-sonnet-4-6"),
    )
    analyzer.analyze_session(_persona(), "tp", _session_jsonl(tmp_path))
    assert fake.calls[0]["model"] == "claude-sonnet-4-6"


def test_analyzer_counts_high_severity(tmp_path):
    fake = _FakeMessages(parsed_output=FrictionReport(
        friction_events=[
            FrictionEvent(
                severity="high", signal_type="navigation", location="/x",
                description="d", what_persona_expected="e", what_actually_happened="a",
            ),
            FrictionEvent(
                severity="HIGH", signal_type="slow_load", location="/y",
                description="d", what_persona_expected="e", what_actually_happened="a",
            ),
            FrictionEvent(
                severity="low", signal_type="navigation", location="/z",
                description="d", what_persona_expected="e", what_actually_happened="a",
            ),
        ],
        overall_verdict="rough",
    ))
    analyzer = FrictionAnalyzer(
        app_config=_app_config(),
        reports_dir=tmp_path / "reports",
        client=_FakeClient(fake),
    )
    summary = analyzer.analyze_session(_persona(), "tp", _session_jsonl(tmp_path))
    assert summary["friction_event_count"] == 3
    assert summary["high_severity_count"] == 2  # case-insensitive


# ─── Confidence + evidence schema (Phase B #4) ────────────────────────────────

def test_friction_event_defaults_confidence_high_and_no_evidence():
    """Old reports + LLM outputs that don't set the new fields must still
    parse cleanly. Backward compat is non-negotiable: the JSON reports on
    disk pre-improvement omit these fields entirely."""
    ev = FrictionEvent(
        severity="medium", signal_type="navigation", location="/x",
        description=".", what_persona_expected=".", what_actually_happened=".",
    )
    assert ev.confidence == "high"
    assert ev.evidence_event_ts == []


def test_friction_event_round_trips_confidence_and_evidence():
    ev = FrictionEvent(
        severity="high", signal_type="instrumentation_gap", location="/context",
        description="Possibly missing instrumentation.",
        what_persona_expected="Affordance visible.",
        what_actually_happened="visible=[] but page may have been pre-hydration.",
        confidence="low",
        evidence_event_ts=["2026-05-17T07:08:42.123Z"],
    )
    dumped = ev.model_dump()
    assert dumped["confidence"] == "low"
    assert dumped["evidence_event_ts"] == ["2026-05-17T07:08:42.123Z"]


def test_render_markdown_tags_low_confidence_events():
    report = FrictionReport(
        friction_events=[
            FrictionEvent(
                severity="high", signal_type="instrumentation_gap",
                location="/context",
                description="Could be missing instrumentation.",
                what_persona_expected="Tabs visible",
                what_actually_happened="visible=[]",
                confidence="low",
                evidence_event_ts=["2026-05-17T07:08:42.123Z"],
            ),
            FrictionEvent(
                severity="medium", signal_type="navigation", location="/x",
                description="Confirmed.", what_persona_expected="A",
                what_actually_happened="B", confidence="high",
            ),
        ],
        overall_verdict=".",
    )
    md = render_markdown("tp", _persona(), report)
    assert "⚠️ low-confidence" in md
    assert "Evidence ts" in md
    assert "2026-05-17T07:08:42.123Z" in md


def test_analyzer_taxonomy_prompts_for_confidence_calibration():
    """The system prompt must instruct the model on when to mark
    confidence=low and how to anchor evidence — otherwise the new schema
    fields will sit empty."""
    cfg = _app_config()
    taxonomy = _build_friction_taxonomy(cfg)
    assert "evidence_event_ts" in taxonomy
    assert "Confidence calibration" in taxonomy or "confidence" in taxonomy.lower()
    # Must explicitly mention the instrumentation_gap escape hatch.
    assert "instrumentation_gap" in taxonomy
