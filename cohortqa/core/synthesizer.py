"""Synthesizer — turn N per-persona friction reports into one polish-spec draft.

Reads the most-recent JSON report per persona from ``qa/reports/``,
makes one Claude API call to find cross-persona patterns, and writes
a markdown spec to ``qa/synthesis/polish-spec-draft-<date>.md`` that
you can paste straight into a new Claude Code session as the input
to the next polish round.

Cost shape (Opus 4.7, default):
  ~6 × 1-2K input per report + ~2K shared instruction = ~10K input
  ~5K output (top-10 patterns with fixes + evidence)
  ≈ 10K × $5/M + 5K × $25/M = $0.05 + $0.13 = ~$0.18

One synthesis call per orchestrator run. Default model claude-opus-4-7,
overridable via PERSONALAB_ANTHROPIC_MODEL.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

# Reuse the analyzer's shared constants + client protocol — keeps the
# default-model rule in one place.
from .analyzer import (
    DEFAULT_MODEL,
    _ClientProtocol,
    _default_client,
    _usage_to_dict,
)


DEFAULT_MAX_TOKENS = 8000
DEFAULT_TARGET_PATTERN_COUNT = 10


# ─── Structured output ────────────────────────────────────────────────────────

class FrictionPattern(BaseModel):
    """One cross-persona friction pattern with a proposed fix."""

    title: str = Field(description="Short pattern name, e.g. 'Score reason is invisible'")
    signal_type: str = Field(
        description=(
            "Friction signal type from the app taxonomy "
            "(navigation, scoring_opacity, archetype_confusion, data_density, "
            "missing_action, broken_link, slow_load, empty_state)."
        )
    )
    severity_range: str = Field(
        description="Lowest→highest severity observed across personas, e.g. 'medium → high'"
    )
    personas_affected: list[str] = Field(
        description="Persona ids where this pattern showed up."
    )
    description: str = Field(
        description="What goes wrong, in one or two sentences."
    )
    proposed_fix: str = Field(
        description="What to change. Concrete, paste-able into a spec."
    )
    implementation_approach: str = Field(
        description="Where in the codebase + how. Routes, components, data flow."
    )
    estimated_effort: str = Field(description="S | M | L")
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Short attributions tying the pattern to specific persona reports — "
            "e.g. 'senior-gtm-eng-nyc: pipeline row score 7/10 without reason chip'."
        ),
    )


class PolishSpec(BaseModel):
    """Synthesized polish spec ready to paste into a new Claude session."""

    patterns: list[FrictionPattern] = Field(
        default_factory=list,
        description=(
            "Top friction patterns, ranked by cross-persona impact. "
            "Cap at ~10."
        ),
    )
    overall_summary: str = Field(
        description="2-3 sentence summary of what the next polish round should focus on."
    )


# ─── Prompt construction (pure) ───────────────────────────────────────────────

def _build_synth_system_prompt(app_config: dict[str, Any]) -> str:
    lines = [
        "You are a product manager turning a batch of per-persona friction "
        "reports into a single, prioritised polish spec for the next sprint.",
        "",
        f"## App under review",
        f"{app_config['app']['name']}: {app_config['app']['description'].strip()}",
        "",
        "## Friction signal taxonomy",
    ]
    for sig in app_config.get("friction_signals", []):
        desc = " ".join(sig.get("description", "").split())
        lines.append(f"- **{sig['type']}** — {desc}")
    lines += [
        "",
        "## Synthesis discipline",
        "- Find PATTERNS — issues appearing in 2+ personas. Single-persona "
        "  nits are deprioritised unless severity is high.",
        "- Group by signal_type so the resulting spec is scannable.",
        "- Rank patterns by cross-persona impact × severity. Cap at ~10.",
        "- For each pattern: title, signal_type, severity_range, "
        "  personas_affected, description, proposed_fix, "
        "  implementation_approach (concrete code/route hints), "
        "  estimated_effort (S/M/L), evidence (short attribution lines).",
        "- evidence MUST tie each pattern back to specific personas — quote "
        "  or paraphrase from the underlying friction events.",
        "- Effort scale: S = a few hours; M = a day; L = multi-day or "
        "  cross-cutting.",
        "- Write proposed_fix and implementation_approach so they can be "
        "  pasted into a new Claude Code session as the next polish brief — "
        "  concrete enough to act on without re-reading the source reports.",
    ]
    return "\n".join(lines)


def _build_synth_user_message(
    reports: list[dict[str, Any]],
    target_pattern_count: int,
) -> str:
    """User-facing payload: the per-persona friction reports as JSON +
    a single ask line."""
    parts = [
        f"Synthesize the top {target_pattern_count} friction patterns from "
        "these per-persona reports. Return a PolishSpec per the schema.",
        "",
        "## Per-persona reports",
    ]
    for r in reports:
        parts += [
            "",
            f"### Persona: `{r['persona_id']}`",
            f"_(report file: {r['report_path']})_",
            "",
            "```json",
            json.dumps(r["report"], ensure_ascii=False, indent=2),
            "```",
        ]
    return "\n".join(parts)


# ─── Report discovery (pure) ──────────────────────────────────────────────────

def find_latest_reports_per_persona(reports_dir: str | Path) -> list[dict[str, Any]]:
    """Walk ``reports_dir``, return the most-recent ``.json`` per persona.

    Persona id is the part of the filename before the timestamp slug —
    we assume the analyzer's ``<persona-id>-<YYYYMMDDTHHMMSSZ>.json``
    naming. The persona id can contain hyphens; we strip exactly the
    last component (the timestamp).
    """
    d = Path(reports_dir)
    if not d.exists():
        return []
    found: dict[str, tuple[float, Path]] = {}
    for p in d.glob("*.json"):
        stem = p.stem  # e.g. senior-gtm-eng-nyc-20260517T084200Z
        if "-" not in stem:
            persona_id = stem
        else:
            persona_id = stem.rsplit("-", 1)[0]
        mtime = p.stat().st_mtime
        if persona_id not in found or found[persona_id][0] < mtime:
            found[persona_id] = (mtime, p)

    out: list[dict[str, Any]] = []
    for persona_id, (mtime, path) in sorted(found.items()):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "persona_id": persona_id,
            "report_path": str(path),
            "report_mtime": mtime,
            "report": report,
        })
    return out


# ─── Markdown rendering (pure) ────────────────────────────────────────────────

def render_polish_spec(app_config: dict[str, Any], spec: PolishSpec,
                       source_reports: list[dict[str, Any]]) -> str:
    app_name = app_config["app"]["name"]
    lines = [
        f"# {app_name} polish spec — draft",
        f"_generated by PersonaLab synthesizer · {_iso_now()}_",
        "",
        f"Synthesized from {len(source_reports)} persona report"
        + ("s" if len(source_reports) != 1 else "")
        + ".",
        "",
        "## Overall summary",
        spec.overall_summary.strip() or "_(no summary returned)_",
        "",
        "## Pattern index",
    ]
    by_signal: dict[str, list[FrictionPattern]] = {}
    for p in spec.patterns:
        by_signal.setdefault(p.signal_type, []).append(p)

    for st in sorted(by_signal.keys()):
        lines.append(f"- **{st}** — {len(by_signal[st])} pattern(s)")
    lines.append("")

    lines.append("## Patterns")
    for i, p in enumerate(spec.patterns, 1):
        lines += [
            "",
            f"### {i}. {p.title} _({p.signal_type})_",
            f"**Severity:** {p.severity_range}  ·  "
            f"**Effort:** {p.estimated_effort}  ·  "
            f"**Personas affected:** {', '.join(p.personas_affected) or '_n/a_'}",
            "",
            p.description.strip(),
            "",
            "**Proposed fix:** " + p.proposed_fix.strip(),
            "",
            "**Implementation approach:** " + p.implementation_approach.strip(),
        ]
        if p.evidence:
            lines.append("")
            lines.append("**Evidence:**")
            for ev in p.evidence:
                lines.append(f"- {ev}")

    lines += [
        "",
        "---",
        "## Source reports",
    ]
    for r in source_reports:
        lines.append(f"- `{r['persona_id']}` → `{r['report_path']}`")
    return "\n".join(lines) + "\n"


# ─── Synthesizer ──────────────────────────────────────────────────────────────

@dataclass
class SynthesizerConfig:
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    target_pattern_count: int = DEFAULT_TARGET_PATTERN_COUNT
    synthesis_dir: Path = field(default_factory=lambda: Path("qa/synthesis"))


class Synthesizer:
    """Drives one synthesis call against a directory of report JSONs."""

    def __init__(
        self,
        app_config: dict[str, Any],
        reports_dir: str | Path,
        synthesis_dir: str | Path | None = None,
        client: _ClientProtocol | None = None,
        config: SynthesizerConfig | None = None,
    ) -> None:
        self.app_config = app_config
        self.reports_dir = Path(reports_dir)
        self.config = config or SynthesizerConfig()
        if synthesis_dir is not None:
            self.synthesis_dir = Path(synthesis_dir)
        else:
            self.synthesis_dir = self.config.synthesis_dir
        self._client = client
        self._system_prompt = _build_synth_system_prompt(app_config)

    @property
    def client(self) -> _ClientProtocol:
        if self._client is None:
            self._client = _default_client()
        return self._client

    def build_messages_kwargs(
        self,
        reports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        system_blocks = [
            {
                "type": "text",
                "text": self._system_prompt,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user_message = _build_synth_user_message(
            reports, self.config.target_pattern_count
        )
        return {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "thinking": {"type": "adaptive"},
            "system": system_blocks,
            "messages": [{"role": "user", "content": user_message}],
            "output_format": PolishSpec,
        }

    def synthesize(self, reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Call Claude, write the polish spec markdown.

        If ``reports`` is None, discovers the latest per-persona JSON
        reports in ``self.reports_dir``.
        """
        if reports is None:
            reports = find_latest_reports_per_persona(self.reports_dir)
        if not reports:
            raise FileNotFoundError(
                f"No persona reports found in {self.reports_dir!s}. "
                "Run the analyzer first."
            )

        kwargs = self.build_messages_kwargs(reports)
        response = self.client.messages.parse(**kwargs)
        spec = response.parsed_output
        if not isinstance(spec, PolishSpec):
            raise RuntimeError(
                f"Expected PolishSpec, got {type(spec).__name__}"
            )

        self.synthesis_dir.mkdir(parents=True, exist_ok=True)
        slug = _date_slug()
        md_path = self.synthesis_dir / f"polish-spec-draft-{slug}.md"
        json_path = self.synthesis_dir / f"polish-spec-draft-{slug}.json"

        md_path.write_text(
            render_polish_spec(self.app_config, spec, reports),
            encoding="utf-8",
        )
        json_path.write_text(
            json.dumps(spec.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return {
            "spec_md": str(md_path),
            "spec_json": str(json_path),
            "pattern_count": len(spec.patterns),
            "persona_count": len(reports),
            "model": kwargs["model"],
            "usage": _usage_to_dict(getattr(response, "usage", None)),
        }


# ─── Utility ──────────────────────────────────────────────────────────────────

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


__all__ = [
    "DEFAULT_TARGET_PATTERN_COUNT",
    "FrictionPattern",
    "PolishSpec",
    "Synthesizer",
    "SynthesizerConfig",
    "find_latest_reports_per_persona",
    "render_polish_spec",
]
