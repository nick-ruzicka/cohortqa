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
    _summary_references_groups,
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


class _FakeMessagesSequence:
    """Returns a different parsed_output on each successive .parse() call."""
    def __init__(self, outputs: list, usage=None):
        self._outputs = list(outputs)
        self.usage = usage or _FakeUsage()
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        output = self._outputs.pop(0) if self._outputs else PolishSpec(overall_summary="")
        return _FakeResponse(output, self.usage)


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


# ─── Single-root-cause detector (Phase B #5) ─────────────────────────────────

def test_demote_forces_low_for_instrumentation_gap_patterns():
    """Rule 1: instrumentation_gap is by definition advisory. Any pattern
    of that type gets confidence=low regardless of what the LLM said."""
    from personalab.core.synthesizer import demote_low_confidence_patterns

    spec = PolishSpec(
        overall_summary=".",
        patterns=[
            FrictionPattern(
                title="Maybe /context is broken",
                signal_type="instrumentation_gap",
                severity_range="high",
                personas_affected=["a", "b", "c"],
                description=".",
                proposed_fix=".",
                implementation_approach=".",
                estimated_effort="S",
                confidence="high",  # the LLM said high; we override
            ),
        ],
    )
    demoted = demote_low_confidence_patterns(spec, reports=[])
    assert demoted.patterns[0].confidence == "low"


def test_demote_demotes_when_majority_of_findings_are_low_confidence():
    """Rule 2: if more than half of the per-persona friction_events that
    contribute to a pattern (matched on signal_type) carry confidence=low,
    the pattern itself is demoted."""
    from personalab.core.synthesizer import demote_low_confidence_patterns

    spec = PolishSpec(
        overall_summary=".",
        patterns=[
            FrictionPattern(
                title="Dead /context",
                signal_type="empty_state",
                severity_range="high",
                personas_affected=["alpha", "beta", "gamma"],
                description=".",
                proposed_fix=".",
                implementation_approach=".",
                estimated_effort="M",
                confidence="high",
            ),
        ],
    )
    reports = [
        {"persona_id": "alpha", "report": {"friction_events": [
            {"signal_type": "empty_state", "confidence": "low"},
        ]}},
        {"persona_id": "beta", "report": {"friction_events": [
            {"signal_type": "empty_state", "confidence": "low"},
        ]}},
        {"persona_id": "gamma", "report": {"friction_events": [
            {"signal_type": "empty_state", "confidence": "high"},
        ]}},
    ]
    demoted = demote_low_confidence_patterns(spec, reports)
    # 2 of 3 contributing events are low → majority → demote.
    assert demoted.patterns[0].confidence == "low"


def test_demote_leaves_high_confidence_alone_when_findings_are_high():
    """Negative case: when contributing findings are mostly high-confidence,
    don't touch the LLM's setting."""
    from personalab.core.synthesizer import demote_low_confidence_patterns

    spec = PolishSpec(
        overall_summary=".",
        patterns=[
            FrictionPattern(
                title="Real timeout",
                signal_type="slow_load",
                severity_range="high",
                personas_affected=["alpha", "beta"],
                description=".",
                proposed_fix=".",
                implementation_approach=".",
                estimated_effort="L",
                confidence="high",
            ),
        ],
    )
    reports = [
        {"persona_id": "alpha", "report": {"friction_events": [
            {"signal_type": "slow_load", "confidence": "high"},
        ]}},
        {"persona_id": "beta", "report": {"friction_events": [
            {"signal_type": "slow_load", "confidence": "high"},
        ]}},
    ]
    demoted = demote_low_confidence_patterns(spec, reports)
    assert demoted.patterns[0].confidence == "high"


def test_demote_handles_findings_without_explicit_confidence_field():
    """Backward compat: old reports stored before the confidence field
    existed default to 'high' (per FrictionEvent's default). The demoter
    must treat absence-of-confidence as 'high', not as low."""
    from personalab.core.synthesizer import demote_low_confidence_patterns

    spec = PolishSpec(
        overall_summary=".",
        patterns=[
            FrictionPattern(
                title="x", signal_type="navigation", severity_range="medium",
                personas_affected=["alpha"], description=".",
                proposed_fix=".", implementation_approach=".",
                estimated_effort="S", confidence="high",
            ),
        ],
    )
    reports = [
        {"persona_id": "alpha", "report": {"friction_events": [
            # No 'confidence' key at all — old format.
            {"signal_type": "navigation"},
        ]}},
    ]
    demoted = demote_low_confidence_patterns(spec, reports)
    assert demoted.patterns[0].confidence == "high"


def test_demote_ignores_patterns_with_no_matching_contributing_events():
    """Edge case: if a pattern's personas_affected list refers to personas
    whose reports don't contain any matching signal_type events (e.g.,
    LLM aggregated under a different label), the demoter must not crash
    and must not change anything."""
    from personalab.core.synthesizer import demote_low_confidence_patterns

    spec = PolishSpec(
        overall_summary=".",
        patterns=[
            FrictionPattern(
                title="x", signal_type="data_density", severity_range="medium",
                personas_affected=["alpha"], description=".",
                proposed_fix=".", implementation_approach=".",
                estimated_effort="M", confidence="medium",
            ),
        ],
    )
    # alpha's report has no data_density events at all.
    reports = [
        {"persona_id": "alpha", "report": {"friction_events": [
            {"signal_type": "scoring_opacity", "confidence": "high"},
        ]}},
    ]
    demoted = demote_low_confidence_patterns(spec, reports)
    assert demoted.patterns[0].confidence == "medium"  # untouched


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


# ─── _summary_references_groups detection ───────────────────────────────

def test_summary_references_groups_detects_prose_with_groups():
    """Summary that names multiple pattern groups should trigger."""
    summary = (
        "Next polish round should focus on three pattern clusters: "
        "first group is scoring visibility, second group is dead-end "
        "surfaces, third group is performance."
    )
    assert _summary_references_groups(summary) is True


def test_summary_references_groups_ignores_sparse_mentions():
    """A summary that says 'pattern' once should NOT trigger."""
    assert _summary_references_groups("One scoring pattern was found.") is False


def test_summary_references_groups_empty():
    assert _summary_references_groups("") is False
    assert _summary_references_groups(None) is False


# ─── Re-prompt fallback (Phase C) ──────────────────────────────────────

def _prose_only_summary() -> str:
    return (
        "Next polish round should focus on three pattern clusters: "
        "first group is scoring visibility (patterns around score "
        "opacity), second group is dead-end surfaces (patterns on "
        "/context and /today), third group is performance (patterns "
        "around slow load)."
    )


def _recovered_pattern() -> FrictionPattern:
    return FrictionPattern(
        title="Scoring visibility",
        signal_type="scoring_opacity",
        severity_range="medium → high",
        personas_affected=["senior-gtm-eng-nyc"],
        description="Score chip has no reason.",
        proposed_fix="Add reason chip.",
        implementation_approach="PipelineTable.tsx",
        estimated_effort="S",
    )


def test_reprompt_triggers_when_prose_only_and_recovers(tmp_path):
    """When the first call returns 0 patterns but a group-rich summary,
    the synthesizer should make a second call and use its patterns."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "alpha-20260517T080000Z.json").write_text(
        json.dumps(_make_report("alpha", signal_types=["scoring_opacity"]))
    )

    # First call: prose-only (0 patterns, group-rich summary).
    # Second call: structured patterns recovered.
    first_response = PolishSpec(
        overall_summary=_prose_only_summary(),
        patterns=[],
    )
    second_response = PolishSpec(
        overall_summary="Recovered.",
        patterns=[_recovered_pattern()],
    )
    fake = _FakeMessagesSequence([first_response, second_response])

    s = Synthesizer(
        app_config=_app_config(),
        reports_dir=reports_dir,
        synthesis_dir=tmp_path / "synth",
        client=_FakeClient(fake),
    )
    result = s.synthesize()

    # Two API calls made.
    assert len(fake.calls) == 2
    # Second call's user message references the summary.
    assert "scoring visibility" in fake.calls[1]["messages"][0]["content"].lower()
    # Result uses recovered patterns + original summary.
    assert result["pattern_count"] == 1
    assert result["reprompted"] is True
    # Markdown file documents the re-prompt.
    md = Path(result["spec_md"]).read_text()
    assert "re-prompt fallback" in md
    # Original summary preserved (not replaced by second call's summary).
    assert "three pattern clusters" in md


def test_reprompt_double_failure_proceeds_with_empty(tmp_path):
    """When both calls return 0 patterns, the synthesizer should not crash
    and should write a valid (empty) spec."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "alpha-20260517T080000Z.json").write_text(
        json.dumps(_make_report("alpha", signal_types=["navigation"]))
    )

    first_response = PolishSpec(
        overall_summary=_prose_only_summary(),
        patterns=[],
    )
    second_response = PolishSpec(
        overall_summary="Still nothing.",
        patterns=[],
    )
    fake = _FakeMessagesSequence([first_response, second_response])

    s = Synthesizer(
        app_config=_app_config(),
        reports_dir=reports_dir,
        synthesis_dir=tmp_path / "synth",
        client=_FakeClient(fake),
    )
    result = s.synthesize()

    assert len(fake.calls) == 2
    assert result["pattern_count"] == 0
    assert result["reprompted"] is False  # didn't successfully recover
    # Still writes valid output files.
    assert Path(result["spec_md"]).exists()
    assert Path(result["spec_json"]).exists()


def test_reprompt_not_triggered_for_non_group_empty_spec(tmp_path):
    """When 0 patterns AND the summary doesn't reference groups,
    no re-prompt should happen (legitimately empty result)."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "alpha-20260517T080000Z.json").write_text(
        json.dumps(_make_report("alpha", signal_types=["navigation"]))
    )

    spec = PolishSpec(
        overall_summary="No significant friction found across personas.",
        patterns=[],
    )
    fake = _FakeMessages(parsed_output=spec)

    s = Synthesizer(
        app_config=_app_config(),
        reports_dir=reports_dir,
        synthesis_dir=tmp_path / "synth",
        client=_FakeClient(fake),
    )
    result = s.synthesize()

    # Only one call — no re-prompt.
    assert len(fake.calls) == 1
    assert result["pattern_count"] == 0
    assert result["reprompted"] is False


def test_reprompt_api_exception_does_not_crash(tmp_path):
    """If the re-prompt call raises, the synthesizer should log and
    continue with the empty spec rather than crashing."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "alpha-20260517T080000Z.json").write_text(
        json.dumps(_make_report("alpha", signal_types=["navigation"]))
    )

    class _ExplodingMessages:
        def __init__(self):
            self.calls: list[dict] = []
            self._call_count = 0
        def parse(self, **kwargs):
            self.calls.append(kwargs)
            self._call_count += 1
            if self._call_count == 1:
                # First call succeeds with prose-only.
                return _FakeResponse(
                    PolishSpec(overall_summary=_prose_only_summary(), patterns=[]),
                    _FakeUsage(),
                )
            # Second call explodes.
            raise RuntimeError("API connection lost")

    fake = _ExplodingMessages()
    s = Synthesizer(
        app_config=_app_config(),
        reports_dir=reports_dir,
        synthesis_dir=tmp_path / "synth",
        client=_FakeClient(fake),
    )
    result = s.synthesize()

    assert len(fake.calls) == 2
    assert result["pattern_count"] == 0
    assert result["reprompted"] is False
