"""Tests for the orchestrator.

Drives the orchestrator end-to-end with both Playwright and Anthropic
faked out — no browser launches, no API calls. Verifies the pipeline
shape (discovery → sessions → reports → synthesis) and the dependency-
injection seams.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cohortqa.core.analyzer import FrictionEvent, FrictionReport
from cohortqa.core.orchestrator import Orchestrator, OrchestratorResult
from cohortqa.core.synthesizer import PolishSpec


# ─── Fixture builders ─────────────────────────────────────────────────────────

PERSONA_YAML = """
identity:
  name: Test Persona
  role: Test
  background: Synthetic.
target_archetypes: [t]
location_preferences: [remote]
comp_floor: 100000
behavioral:
  click_speed: medium
  reads_details: true
  rejection_threshold: medium
  detail_dwell_ms: 1000
meta_attitude: Testing.
friction_sensitivities: [navigation]
"""


def _write_app(tmp_path: Path) -> Path:
    """Minimal valid app.yaml + dirs."""
    (tmp_path / "personas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    app_yaml = tmp_path / "app.yaml"
    app_yaml.write_text("""
app:
  name: SmokeApp
  dev_server: http://localhost:3000
  description: Smoke.
routes:
  - path: /x
    purpose: x
    actions: [click_x]
actions:
  - name: click_x
    selector: '[data-x]'
friction_signals:
  - type: navigation
    description: Lost.
personas_dir: personas
scenarios_dir: scenarios
runs_dir: runs
""")
    return app_yaml


def _write_personas(tmp_path: Path, names: list[str]) -> None:
    for n in names:
        (tmp_path / "personas" / f"{n}.yaml").write_text(PERSONA_YAML)


# ─── Fake session runner (no Playwright) ──────────────────────────────────────

def _make_fake_session_runner():
    """Returns a SessionRunner that writes a synthetic JSONL session for
    each persona. Exposes ``.calls`` so tests can assert dispatch order +
    parallelism."""
    calls: list[dict[str, Any]] = []

    async def runner(
        *,
        persona: dict[str, Any],
        persona_id: str,
        app_config: dict[str, Any],
        runs_dir: Path,
        app_config_dir: Path,
        headless: bool,
        dev_server_override: str | None,
    ) -> dict[str, Any]:
        calls.append({
            "persona_id": persona_id,
            "headless": headless,
            "dev_server_override": dev_server_override,
        })
        session_path = Path(runs_dir) / f"{persona_id}-fake.jsonl"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {"event_type": "reasoning", "reasoning": "opening"},
            {"event_type": "nav", "route": "/x", "render_time_ms": 100,
             "page_state": {"visible_action_names": [], "console_errors": []}},
            {"event_type": "action", "route": "/x", "action": "click_x"},
        ]
        session_path.write_text(
            "\n".join(json.dumps(e) for e in events) + "\n",
            encoding="utf-8",
        )
        return {
            "persona_id": persona_id,
            "session_path": str(session_path),
            "routes_visited": 1,
            "actions_taken": 1,
            "events": 3,
            "errors": 0,
        }

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ─── Fake analyzer + synthesizer ──────────────────────────────────────────────

class _FakeAnalyzer:
    """Stands in for FrictionAnalyzer. Writes a minimal report per call so
    the synthesizer's finder can pick it up."""

    def __init__(self, reports_dir: Path):
        self.reports_dir = reports_dir
        self.calls: list[str] = []

    def analyze_session(self, persona, persona_id, session_path):
        self.calls.append(persona_id)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.reports_dir / f"{persona_id}-fake.json"
        report_path.write_text(json.dumps({
            "overall_verdict": f"{persona_id} verdict",
            "friction_events": [{
                "severity": "medium",
                "signal_type": "navigation",
                "location": "/x",
                "description": f"{persona_id} hit navigation",
                "what_persona_expected": "e",
                "what_actually_happened": "a",
            }],
            "ux_issues": [],
            "opportunities": [],
            "wins": [],
        }))
        return {
            "persona_id": persona_id,
            "session_path": str(session_path),
            "report_md": str(report_path).replace(".json", ".md"),
            "report_json": str(report_path),
            "friction_event_count": 1,
            "high_severity_count": 0,
            "model": "fake",
            "usage": None,
        }


class _FakeSynthesizer:
    def __init__(self):
        self.calls = 0

    def synthesize(self):
        self.calls += 1
        return {
            "spec_md": "/synth/spec.md",
            "spec_json": "/synth/spec.json",
            "pattern_count": 2,
            "persona_count": 3,
            "model": "fake",
            "usage": None,
        }


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_orchestrator_init_resolves_dirs_from_app_config(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["alpha"])
    o = Orchestrator(app_config_path=app_yaml)
    assert o.personas_dir == tmp_path / "personas"
    assert o.runs_dir == tmp_path / "runs"
    assert o.reports_dir == tmp_path / "reports"
    assert o.synthesis_dir == tmp_path / "synthesis"


def test_discover_personas_finds_yaml_files_sorted(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["zeta", "alpha", "mike"])
    o = Orchestrator(app_config_path=app_yaml)
    personas = o.discover_personas()
    assert [pid for pid, _ in personas] == ["alpha", "mike", "zeta"]
    # Each is a fully loaded persona dict, not just the path.
    assert all("identity" in p for _, p in personas)


def test_discover_raises_when_no_personas(tmp_path):
    app_yaml = _write_app(tmp_path)
    o = Orchestrator(app_config_path=app_yaml)
    with pytest.raises(FileNotFoundError):
        o.discover_personas()


def test_full_pipeline_with_all_fakes(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["alpha", "beta", "gamma"])

    session_runner = _make_fake_session_runner()
    analyzer = _FakeAnalyzer(reports_dir=tmp_path / "reports")
    synthesizer = _FakeSynthesizer()

    o = Orchestrator(
        app_config_path=app_yaml,
        parallel=2,
        analyzer=analyzer,
        synthesizer=synthesizer,
        session_runner=session_runner,
    )
    result = asyncio.run(o.run())

    assert isinstance(result, OrchestratorResult)
    assert len(result.sessions) == 3
    assert {s["persona_id"] for s in result.sessions} == {"alpha", "beta", "gamma"}
    assert len(result.reports) == 3
    assert analyzer.calls == ["alpha", "beta", "gamma"]
    assert synthesizer.calls == 1
    assert result.synthesis is not None
    assert result.elapsed_seconds >= 0


def test_skip_analysis_skips_both_analysis_and_synthesis(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["alpha"])
    session_runner = _make_fake_session_runner()
    analyzer = _FakeAnalyzer(reports_dir=tmp_path / "reports")
    synthesizer = _FakeSynthesizer()

    o = Orchestrator(
        app_config_path=app_yaml,
        skip_analysis=True,
        analyzer=analyzer,
        synthesizer=synthesizer,
        session_runner=session_runner,
    )
    result = asyncio.run(o.run())
    assert result.reports == []
    assert result.synthesis is None
    assert analyzer.calls == []
    assert synthesizer.calls == 0


def test_skip_synthesis_keeps_analysis(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["alpha"])
    session_runner = _make_fake_session_runner()
    analyzer = _FakeAnalyzer(reports_dir=tmp_path / "reports")
    synthesizer = _FakeSynthesizer()

    o = Orchestrator(
        app_config_path=app_yaml,
        skip_synthesis=True,
        analyzer=analyzer,
        synthesizer=synthesizer,
        session_runner=session_runner,
    )
    result = asyncio.run(o.run())
    assert len(result.reports) == 1
    assert result.synthesis is None
    assert synthesizer.calls == 0


def test_parallel_batches_dispatch_in_chunks(tmp_path):
    """With parallel=2 and 5 personas, runner is called 3 batches × 2/2/1."""
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["a", "b", "c", "d", "e"])
    session_runner = _make_fake_session_runner()

    o = Orchestrator(
        app_config_path=app_yaml,
        parallel=2,
        skip_analysis=True,
        session_runner=session_runner,
    )
    asyncio.run(o.run())
    # All 5 personas should have been dispatched.
    assert len(session_runner.calls) == 5
    assert {c["persona_id"] for c in session_runner.calls} == {"a", "b", "c", "d", "e"}


def test_dev_server_override_propagates_to_session_runner(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["alpha"])
    session_runner = _make_fake_session_runner()

    o = Orchestrator(
        app_config_path=app_yaml,
        skip_analysis=True,
        dev_server_override="http://localhost:9999",
        session_runner=session_runner,
    )
    asyncio.run(o.run())
    assert session_runner.calls[0]["dev_server_override"] == "http://localhost:9999"


def test_headless_flag_propagates(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["alpha"])
    session_runner = _make_fake_session_runner()

    o = Orchestrator(
        app_config_path=app_yaml,
        skip_analysis=True,
        headless=False,
        session_runner=session_runner,
    )
    asyncio.run(o.run())
    assert session_runner.calls[0]["headless"] is False


def test_result_to_dict_is_json_serialisable(tmp_path):
    app_yaml = _write_app(tmp_path)
    _write_personas(tmp_path, ["alpha"])
    session_runner = _make_fake_session_runner()
    analyzer = _FakeAnalyzer(reports_dir=tmp_path / "reports")
    synthesizer = _FakeSynthesizer()

    o = Orchestrator(
        app_config_path=app_yaml,
        analyzer=analyzer,
        synthesizer=synthesizer,
        session_runner=session_runner,
    )
    result = asyncio.run(o.run())
    # If anything in here is non-serialisable, this throws.
    json.dumps(result.to_dict())


# ─── CLI smoke ────────────────────────────────────────────────────────────────

def test_cli_argparser_accepts_minimal_args():
    """The CLI shouldn't reject the documented invocation."""
    from cohortqa.core.orchestrator import _build_argparser
    parser = _build_argparser()
    args = parser.parse_args(["--app", "qa/app.yaml"])
    assert args.app == "qa/app.yaml"
    assert args.parallel == 6
    assert args.skip_analysis is False
    assert args.skip_synthesis is False


def test_cli_argparser_accepts_full_flag_set():
    from cohortqa.core.orchestrator import _build_argparser
    parser = _build_argparser()
    args = parser.parse_args([
        "--app", "qa/app.yaml",
        "--parallel", "3",
        "--dev-server", "http://localhost:9999",
        "--no-headless",
        "--skip-analysis",
        "--json",
    ])
    assert args.parallel == 3
    assert args.dev_server == "http://localhost:9999"
    assert args.no_headless is True
    assert args.skip_analysis is True
    assert args.json is True
