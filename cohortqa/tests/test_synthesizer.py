"""Tests for the synthesizer. Mocked Anthropic client, no real API calls."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from personalab.core.synthesizer import (
    DEFAULT_TARGET_PATTERN_COUNT,
    FrictionPattern,
    PolishSpec,
    Synthesizer,
    SynthesizerConfig,
    _build_synth_system_prompt,
    _build_synth_user_message,
    find_latest_reports_per_persona,
    render_polish_spec,
)


def _app_config() -> dict:
    return {
        "app": {"name": "App", "dev_server": "http://x", "description": "A test app."},
        "routes": [{"path": "/x", "purpose": ".", "actions": []}],
        "actions": [],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
            {"type": "scoring_opacity", "description": "Unclear score."},
        ],
        "personas_dir": "p", "scenarios_dir": "s", "runs_dir": "r",
    }


def _make_report(persona_id: str, *, signal_types: list[str]) -> dict:
    """Synthetic report shaped like FrictionAnalyzer would write."""
    return {
        "overall_verdict": f"{persona_id} verdict.",
        "friction_events": [
            {
                "severity": "medium",
                "signal_type": st,
                "location": f"/x — {st}",
                "description": f"{persona_id} hit {st}.",
                "what_persona_expected": "smoother",
                "what_actually_happened": "rough",
            }
            for st in signal_types
        ],
        "ux_issues": [],
        "opportunities": [],
        "wins": [],
    }


# ─── Fakes ────────────────────────────────────────────────────────────────────

class _FakeUsage:
    def __init__(self, **kw):
        self.input_tokens = kw.get("input_tokens", 1000)
        self.output_tokens = kw.get("output_tokens", 500)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 800)


class _FakeResponse:
    def __init__(self, parsed_output, usage):
        self.parsed_output = parsed_output
        self.usage = usage


class _FakeMessages:
    def __init__(self, parsed_output, usage=None):
        self.parsed_output = parsed_output
        self.usage = usage or _FakeUsage()
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.parsed_output, self.usage)


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


# ─── find_latest_reports_per_persona ─────────────────────────────────────────

def test_finder_picks_latest_per_persona(tmp_path):
    # Two reports for one persona — newer should win.
    reports = tmp_path / "reports"
    reports.mkdir()
    old = reports / "senior-gtm-eng-nyc-20260101T000000Z.json"
    new = reports / "senior-gtm-eng-nyc-20260517T084200Z.json"
    other = reports / "mid-revops-crossover-20260517T084200Z.json"
    old.write_text(json.dumps(_make_report("senior-gtm-eng-nyc", signal_types=["navigation"])))
    time.sleep(0.01)  # ensure mtime ordering
    new.write_text(json.dumps(_make_report("senior-gtm-eng-nyc", signal_types=["scoring_opacity"])))
    other.write_text(json.dumps(_make_report("mid-revops-crossover", signal_types=["navigation"])))

    found = find_latest_reports_per_persona(reports)
    by_id = {r["persona_id"]: r for r in found}
    assert set(by_id.keys()) == {"senior-gtm-eng-nyc", "mid-revops-crossover"}
    # Newer file picked.
    assert by_id["senior-gtm-eng-nyc"]["report_path"].endswith(new.name)
    # Loaded report content.
    assert by_id["senior-gtm-eng-nyc"]["report"]["friction_events"][0]["signal_type"] == "scoring_opacity"


def test_finder_handles_missing_dir(tmp_path):
    assert find_latest_reports_per_persona(tmp_path / "nonexistent") == []


def test_finder_skips_malformed_json(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "ok-20260517T084200Z.json").write_text(
        json.dumps(_make_report("ok", signal_types=["navigation"]))
    )
    (reports / "bad-20260517T084200Z.json").write_text("{ not valid json")
    found = find_latest_reports_per_persona(reports)
    assert [r["persona_id"] for r in found] == ["ok"]


def test_finder_handles_persona_ids_with_hyphens(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    name = "senior-gtm-eng-nyc-20260517T084200Z.json"
    (reports / name).write_text(json.dumps(_make_report("senior-gtm-eng-nyc", signal_types=["navigation"])))
    found = find_latest_reports_per_persona(reports)
    assert found[0]["persona_id"] == "senior-gtm-eng-nyc"


# ─── Prompt construction ─────────────────────────────────────────────────────

def test_system_prompt_includes_taxonomy_and_discipline():
    s = _build_synth_system_prompt(_app_config())
    assert "App: A test app." in s
    assert "**navigation**" in s
    assert "**scoring_opacity**" in s
    assert "PATTERNS" in s
    assert "S = a few hours" in s
    assert "estimated_effort" in s


def test_user_message_includes_every_persona_report():
    reports = [
        {
            "persona_id": "alpha",
            "report_path": "/r/a.json",
            "report": _make_report("alpha", signal_types=["navigation"]),
        },
        {
            "persona_id": "beta",
            "report_path": "/r/b.json",
            "report": _make_report("beta", signal_types=["scoring_opacity"]),
        },
    ]
    msg = _build_synth_user_message(reports, target_pattern_count=10)
    assert "alpha" in msg and "beta" in msg
    assert "top 10 friction patterns" in msg
    assert "scoring_opacity" in msg


# ─── render_polish_spec ──────────────────────────────────────────────────────

def test_render_groups_by_signal_type_in_index():
    spec = PolishSpec(
        overall_summary="Focus on scoring.",
        patterns=[
            FrictionPattern(
                title="Score reason invisible",
                signal_type="scoring_opacity",
                severity_range="medium → high",
                personas_affected=["senior-gtm-eng-nyc", "mid-revops-crossover"],
                description="Score chip shows /10 with no reason.",
                proposed_fix="Add reason chip beneath score.",
                implementation_approach="dashboard-web/components/PipelineTable.tsx",
                estimated_effort="S",
                evidence=["senior-gtm-eng-nyc: 7/10 with no reason"],
            ),
            FrictionPattern(
                title="No back to companies",
                signal_type="navigation",
                severity_range="low → medium",
                personas_affected=["ambivalent-explorer"],
                description="Drilldown has no back link.",
                proposed_fix="Add back link.",
                implementation_approach="app/companies/[slug]/page.tsx",
                estimated_effort="S",
                evidence=[],
            ),
        ],
    )
    md = render_polish_spec(_app_config(), spec, [
        {"persona_id": "p1", "report_path": "/r/p1.json", "report": {}},
    ])
    assert "## Pattern index" in md
    assert "**navigation** — 1 pattern" in md
    assert "**scoring_opacity** — 1 pattern" in md
    assert "Focus on scoring." in md
    assert "### 1. Score reason invisible" in md
    # Evidence rendered when present
    assert "senior-gtm-eng-nyc: 7/10 with no reason" in md


def test_render_handles_empty_spec_gracefully():
    spec = PolishSpec(overall_summary="Nothing to fix.")
    md = render_polish_spec(_app_config(), spec, [])
    assert "Nothing to fix." in md


# ─── Synthesizer end-to-end (fake client) ────────────────────────────────────

def test_synthesize_with_fake_client_writes_md_and_json(tmp_path):
    # Stage two reports on disk.
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "alpha-20260517T080000Z.json").write_text(
        json.dumps(_make_report("alpha", signal_types=["navigation"]))
    )
    (reports_dir / "beta-20260517T080100Z.json").write_text(
        json.dumps(_make_report("beta", signal_types=["scoring_opacity"]))
    )

    expected_spec = PolishSpec(
        overall_summary="Two patterns surfaced.",
        patterns=[
            FrictionPattern(
                title="Lost on /companies",
                signal_type="navigation",
                severity_range="medium",
                personas_affected=["alpha"],
                description="No back affordance.",
                proposed_fix="Add breadcrumb.",
                implementation_approach="app/companies/[slug]/page.tsx",
                estimated_effort="S",
            ),
        ],
    )
    fake = _FakeMessages(parsed_output=expected_spec)

    s = Synthesizer(
        app_config=_app_config(),
        reports_dir=reports_dir,
        synthesis_dir=tmp_path / "synth",
        client=_FakeClient(fake),
    )
    summary = s.synthesize()

    md = Path(summary["spec_md"]).read_text()
    assert "Two patterns surfaced." in md
    assert "Lost on /companies" in md

    parsed = json.loads(Path(summary["spec_json"]).read_text())
    assert parsed["overall_summary"] == "Two patterns surfaced."
    assert parsed["patterns"][0]["title"] == "Lost on /companies"

    assert summary["pattern_count"] == 1
    assert summary["persona_count"] == 2
    assert summary["model"]


def test_synthesize_raises_when_no_reports(tmp_path):
    s = Synthesizer(
        app_config=_app_config(),
        reports_dir=tmp_path / "empty",
        synthesis_dir=tmp_path / "synth",
        client=_FakeClient(_FakeMessages(parsed_output=PolishSpec(overall_summary=""))),
    )
    with pytest.raises(FileNotFoundError):
        s.synthesize()


def test_synthesizer_passes_adaptive_thinking_and_caches_system(tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "alpha-20260517T080000Z.json").write_text(
        json.dumps(_make_report("alpha", signal_types=["navigation"]))
    )
    fake = _FakeMessages(parsed_output=PolishSpec(overall_summary="ok"))
    s = Synthesizer(
        app_config=_app_config(),
        reports_dir=reports_dir,
        synthesis_dir=tmp_path / "synth",
        client=_FakeClient(fake),
    )
    s.synthesize()
    kwargs = fake.calls[0]
    assert kwargs["thinking"] == {"type": "adaptive"}
    for forbidden in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert forbidden not in kwargs
    assert kwargs["output_format"] is PolishSpec
    # The single system block must be cache-controlled.
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_default_target_pattern_count_is_ten():
    assert DEFAULT_TARGET_PATTERN_COUNT == 10
    assert SynthesizerConfig().target_pattern_count == 10


# ─── Confidence on FrictionPattern (Phase B #4) ──────────────────────────────

def test_friction_pattern_defaults_confidence_high():
    """Backward compat: existing reports/specs don't carry a confidence
    field. The model must default to 'high' rather than erroring or
    silently treating absence as low."""
    p = FrictionPattern(
        title="x", signal_type="navigation", severity_range="medium",
        personas_affected=["a"], description=".",
        proposed_fix=".", implementation_approach=".",
        estimated_effort="S",
    )
    assert p.confidence == "high"


def test_render_polish_spec_tags_low_confidence_patterns():
    spec = PolishSpec(
        overall_summary="Mostly real, one suspect.",
        patterns=[
            FrictionPattern(
                title="Real timeout",
                signal_type="slow_load",
                severity_range="high",
                personas_affected=["a", "b", "c"],
                description="Pipeline timed out for three personas.",
                proposed_fix="Profile /pipeline route handler.",
                implementation_approach="dashboard-web/app/pipeline/",
                estimated_effort="L",
                confidence="high",
            ),
            FrictionPattern(
                title="Possibly instrumented",
                signal_type="instrumentation_gap",
                severity_range="high",
                personas_affected=["a", "b", "c", "d", "e", "f"],
                description="All six saw visible=[]; may be selector stale.",
                proposed_fix="Verify selectors against actual /context.",
                implementation_approach="qa/app.yaml selectors for /context.",
                estimated_effort="S",
                confidence="low",
            ),
        ],
    )
    md = render_polish_spec(_app_config(), spec, [
        {"persona_id": "p", "report_path": "/r/p.json", "report": {}},
    ])
    # Low-confidence pattern is visibly tagged.
    assert "⚠️ low-confidence" in md
    assert "verify before building" in md
    # High-confidence pattern is NOT tagged.
    lines = md.splitlines()
    real_header = next(line for line in lines if "Real timeout" in line)
    assert "⚠️" not in real_header


def test_synth_system_prompt_instructs_on_confidence():
    """The prompt must teach the model about the confidence-low rule for
    instrumentation_gap and single-shared-signal patterns. Otherwise the
    new field will sit at default 'high' forever."""
    cfg = _app_config()
    prompt = _build_synth_system_prompt(cfg)
    assert "confidence" in prompt
    # Must call out the single-root-cause amplifier risk explicitly.
    assert "one root cause counted" in prompt or "single shared session" in prompt.lower() \
        or "instrumentation_gap" in prompt
    # No padding floor anymore.
    assert "as the evidence supports" in prompt
    assert "Do not pad" in prompt
