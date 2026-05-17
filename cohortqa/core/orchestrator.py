"""PersonaLab orchestrator — one CLI invocation runs the whole loop.

Pipeline:

  1. Load app.yaml and every persona under ``personas_dir`` (resolved
     relative to the app.yaml's directory unless overridden).
  2. Run each persona's session in parallel via ``asyncio.gather`` —
     each PersonaRunner gets its own headless browser context, so
     parallelism is safe up to the user's machine + dev server limits.
  3. Analyze every session (sequentially, to keep the analyzer's
     prompt cache warm — the friction taxonomy block reads from
     cache on calls 2-N within the 5-minute TTL).
  4. Synthesize one polish-spec draft from all reports.
  5. Print a summary, return the structured result.

CLI:

  python -m personalab.core.orchestrator --app qa/app.yaml
  python -m personalab.core.orchestrator --app qa/app.yaml \
      --parallel 3 --skip-analysis --skip-synthesis

The CLI exposes flags for the common knobs (parallelism, dev_server
override, skipping the LLM-spending steps). The Python API takes more
— see ``Orchestrator.__init__`` — for tests + programmatic use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .analyzer import FrictionAnalyzer
from .persona_schema import load_app_config, load_persona
from .runner import PersonaRunner
from .synthesizer import Synthesizer


# ─── Dependency-injection seam ───────────────────────────────────────────────

# A SessionRunner takes (persona, persona_id, app_config, runs_dir,
# app_config_dir, dev_server_override, headless) and returns the
# PersonaRunner's session summary. Tests pass a fake that synthesises
# a session log without touching Playwright.
SessionRunner = Callable[..., Awaitable[dict[str, Any]]]


async def _default_session_runner(
    *,
    persona: dict[str, Any],
    persona_id: str,
    app_config: dict[str, Any],
    runs_dir: Path,
    app_config_dir: Path,
    headless: bool,
    dev_server_override: str | None,
) -> dict[str, Any]:
    runner = PersonaRunner(
        persona=persona,
        persona_id=persona_id,
        app_config=app_config,
        runs_dir=runs_dir,
        app_config_dir=app_config_dir,
        headless=headless,
        dev_server_override=dev_server_override,
    )
    return await runner.run()


# ─── Orchestrator ────────────────────────────────────────────────────────────

@dataclass
class OrchestratorResult:
    sessions: list[dict[str, Any]]
    reports: list[dict[str, Any]]
    synthesis: dict[str, Any] | None
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "reports": self.reports,
            "synthesis": self.synthesis,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class Orchestrator:
    """Run PersonaLab end-to-end against one app + its personas."""

    def __init__(
        self,
        app_config_path: str | Path,
        *,
        personas_dir_override: str | Path | None = None,
        runs_dir_override: str | Path | None = None,
        reports_dir_override: str | Path | None = None,
        synthesis_dir_override: str | Path | None = None,
        parallel: int = 6,
        headless: bool = True,
        dev_server_override: str | None = None,
        skip_analysis: bool = False,
        skip_synthesis: bool = False,
        analyzer: FrictionAnalyzer | None = None,
        synthesizer: Synthesizer | None = None,
        session_runner: SessionRunner | None = None,
    ) -> None:
        self.app_config_path = Path(app_config_path).resolve()
        self.app_config_dir = self.app_config_path.parent
        self.app_config = load_app_config(self.app_config_path)

        # Path resolution: app_config declares each dir relative to itself;
        # caller can override per-dir for tests / non-default layouts.
        def _resolve(name: str, override: str | Path | None) -> Path:
            if override is not None:
                return Path(override)
            return self.app_config_dir / self.app_config[name]

        self.personas_dir = (
            Path(personas_dir_override) if personas_dir_override is not None
            else _resolve("personas_dir", None)
        )
        self.runs_dir = _resolve("runs_dir", runs_dir_override)
        self.reports_dir = (
            Path(reports_dir_override) if reports_dir_override is not None
            else self.app_config_dir / "reports"
        )
        self.synthesis_dir = (
            Path(synthesis_dir_override) if synthesis_dir_override is not None
            else self.app_config_dir / "synthesis"
        )

        self.parallel = max(1, parallel)
        self.headless = headless
        self.dev_server_override = dev_server_override
        self.skip_analysis = skip_analysis
        self.skip_synthesis = skip_synthesis

        # Lazy default analyzer/synthesizer so tests can pre-wire fakes
        # without those classes ever instantiating the real Anthropic
        # client.
        self._analyzer = analyzer
        self._synthesizer = synthesizer
        self._session_runner = session_runner or _default_session_runner

    # ─── Discovery ────────────────────────────────────────────────────────────

    def discover_personas(self) -> list[tuple[str, dict[str, Any]]]:
        """Return [(persona_id, persona_dict)] for every YAML under
        ``personas_dir``, sorted by filename for determinism."""
        if not self.personas_dir.exists():
            raise FileNotFoundError(
                f"personas dir not found: {self.personas_dir}"
            )
        out: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(self.personas_dir.glob("*.yaml")):
            persona = load_persona(path)
            out.append((path.stem, persona))
        if not out:
            raise FileNotFoundError(
                f"no persona YAMLs found in {self.personas_dir}"
            )
        return out

    # ─── Phases ───────────────────────────────────────────────────────────────

    async def _run_all_sessions(
        self, personas: list[tuple[str, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Fan out via asyncio.gather, in batches of ``self.parallel``.

        Batching keeps memory in check on small machines (every Chrome
        context is ~100MB). The default 6 + 6 personas means one big
        batch in practice, but `--parallel 3` halves peak memory.
        """
        results: list[dict[str, Any]] = []
        for i in range(0, len(personas), self.parallel):
            batch = personas[i:i + self.parallel]
            coros = [
                self._session_runner(
                    persona=p,
                    persona_id=pid,
                    app_config=self.app_config,
                    runs_dir=self.runs_dir,
                    app_config_dir=self.app_config_dir,
                    headless=self.headless,
                    dev_server_override=self.dev_server_override,
                )
                for pid, p in batch
            ]
            batch_results = await asyncio.gather(*coros, return_exceptions=False)
            results.extend(batch_results)
        return results

    def _build_analyzer(self) -> FrictionAnalyzer:
        if self._analyzer is not None:
            return self._analyzer
        return FrictionAnalyzer(
            app_config=self.app_config,
            reports_dir=self.reports_dir,
        )

    def _build_synthesizer(self) -> Synthesizer:
        if self._synthesizer is not None:
            return self._synthesizer
        return Synthesizer(
            app_config=self.app_config,
            reports_dir=self.reports_dir,
            synthesis_dir=self.synthesis_dir,
        )

    def _analyze_all(
        self,
        personas: list[tuple[str, dict[str, Any]]],
        session_summaries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        analyzer = self._build_analyzer()
        # Sequential by design — keeps the taxonomy block warm in the
        # prompt cache across calls (~5 reads after the first writes it).
        reports: list[dict[str, Any]] = []
        for (persona_id, persona), session in zip(personas, session_summaries):
            report = analyzer.analyze_session(
                persona=persona,
                persona_id=persona_id,
                session_path=session["session_path"],
            )
            reports.append(report)
        return reports

    def _synthesize(self) -> dict[str, Any]:
        return self._build_synthesizer().synthesize()

    # ─── Main entrypoint ──────────────────────────────────────────────────────

    async def run(self) -> OrchestratorResult:
        started = time.monotonic()
        personas = self.discover_personas()

        sessions = await self._run_all_sessions(personas)

        reports: list[dict[str, Any]] = []
        if not self.skip_analysis:
            reports = self._analyze_all(personas, sessions)

        synthesis: dict[str, Any] | None = None
        if not self.skip_synthesis and not self.skip_analysis:
            # Synthesis reads from disk via the reports dir; only meaningful
            # after analysis has written something there.
            synthesis = self._synthesize()

        elapsed = time.monotonic() - started
        return OrchestratorResult(
            sessions=sessions,
            reports=reports,
            synthesis=synthesis,
            elapsed_seconds=elapsed,
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m personalab.core.orchestrator",
        description="Run PersonaLab end-to-end against an app config.",
    )
    p.add_argument(
        "--app", required=True,
        help="Path to the app.yaml (e.g. qa/app.yaml).",
    )
    p.add_argument(
        "--parallel", type=int, default=6,
        help="How many personas to run concurrently (default 6).",
    )
    p.add_argument(
        "--dev-server",
        help="Override the dev_server URL declared in app.yaml.",
    )
    p.add_argument(
        "--no-headless", action="store_true",
        help="Run with a visible browser (debug only).",
    )
    p.add_argument(
        "--skip-analysis", action="store_true",
        help="Run sessions only; don't call the analyzer or synthesizer.",
    )
    p.add_argument(
        "--skip-synthesis", action="store_true",
        help="Run sessions + analysis; don't call the synthesizer.",
    )
    p.add_argument(
        "--reports-dir",
        help="Override the reports output dir (default: <app_yaml_dir>/reports).",
    )
    p.add_argument(
        "--synthesis-dir",
        help="Override the synthesis output dir (default: <app_yaml_dir>/synthesis).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit the full result dict as JSON instead of the summary table.",
    )
    return p


def _print_summary(result: OrchestratorResult) -> None:
    print("\nPersonaLab orchestrator — done")
    print("─" * 40)
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")
    print(f"Sessions: {len(result.sessions)}")
    for s in result.sessions:
        print(
            f"  · {s.get('persona_id', '?')}: "
            f"{s.get('routes_visited', 0)} routes, "
            f"{s.get('actions_taken', 0)} actions"
            + (f"  ({s.get('session_path')})" if s.get("session_path") else "")
        )
    if result.reports:
        print(f"Reports: {len(result.reports)}")
        for r in result.reports:
            print(
                f"  · {r.get('persona_id', '?')}: "
                f"{r.get('friction_event_count', 0)} events "
                f"({r.get('high_severity_count', 0)} high) → "
                f"{r.get('report_md', '?')}"
            )
    if result.synthesis:
        print(
            f"Synthesis: {result.synthesis.get('pattern_count', '?')} patterns "
            f"→ {result.synthesis.get('spec_md', '?')}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    orch = Orchestrator(
        app_config_path=args.app,
        parallel=args.parallel,
        dev_server_override=args.dev_server,
        headless=not args.no_headless,
        skip_analysis=args.skip_analysis,
        skip_synthesis=args.skip_synthesis,
        reports_dir_override=args.reports_dir,
        synthesis_dir_override=args.synthesis_dir,
    )
    result = asyncio.run(orch.run())

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "Orchestrator",
    "OrchestratorResult",
    "main",
]
