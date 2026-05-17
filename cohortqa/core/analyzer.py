"""Friction analyzer — turns a session log into a structured friction report.

Reads a JSONL session produced by ``PersonaRunner``, makes a single Claude
API call with the persona+app context, and writes both a structured JSON
report and a human-readable Markdown report to the app's ``reports_dir``.

The runner *observes*; the analyzer *interprets*. That split keeps the
runner cheap and deterministic (no LLM in the per-session hot path) and
lets the analyzer run async / batched / re-run against existing sessions.

Cost shape per call (Opus 4.7, default):
  ~3-5K input tokens × $5/M  ≈ $0.02
  ~2-4K output tokens × $25/M ≈ $0.075
  ≈ $0.10/call. 6 personas + 1 synthesis = ~$0.70/full-run.

The friction taxonomy in the system prompt is identical across personas
and across sessions of the same app, so it sits before a cache_control
breakpoint. The 6-persona orchestrator run gets ~5 cache hits on the
taxonomy block (first call writes, next five read at ~0.1× price).

API + model defaults come from the claude-api skill: Opus 4.7, adaptive
thinking, no sampling params, structured output via ``messages.parse``.
The model is overridable via the PERSONALAB_ANTHROPIC_MODEL env var if
the user explicitly wants Sonnet/Haiku for cost.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from pydantic import BaseModel, Field

from .behavior import archetype_engagement
from .runner import read_session

# Per the claude-api skill: ALWAYS use claude-opus-4-7 unless the user
# explicitly names a different model. Env override exists for the
# 100-call / $3 budget cap — user opts in by setting it.
DEFAULT_MODEL = os.environ.get("PERSONALAB_ANTHROPIC_MODEL", "claude-opus-4-7")
DEFAULT_MAX_TOKENS = 8000

# ─── Structured output schema ─────────────────────────────────────────────────


class FrictionEvent(BaseModel):
    """One concrete moment where the app failed the persona's expectation."""

    severity: str = Field(description="high | medium | low")
    signal_type: str = Field(
        description=(
            "One of the app-declared friction_signal types: navigation, "
            "scoring_opacity, archetype_confusion, data_density, "
            "missing_action, broken_link, slow_load, empty_state."
        )
    )
    location: str = Field(
        description="Route path + optional element/section, e.g. '/pipeline → row actions'"
    )
    description: str = Field(description="What happened, one or two sentences.")
    what_persona_expected: str = Field(
        description="What the persona's mental model said should happen."
    )
    what_actually_happened: str = Field(
        description="What the runner actually observed."
    )


class UxIssue(BaseModel):
    severity: str
    description: str
    location: str


class Opportunity(BaseModel):
    """A productive direction the analyzer found while watching the session."""

    description: str
    what_user_wanted: str
    why_blocked: str


class Win(BaseModel):
    """Something the app got right for this persona."""

    description: str
    location: str


class FrictionReport(BaseModel):
    """The structured output of one session analysis."""

    friction_events: list[FrictionEvent] = Field(default_factory=list)
    ux_issues: list[UxIssue] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)
    wins: list[Win] = Field(default_factory=list)
    overall_verdict: str = Field(
        description=(
            "One paragraph summarising the persona's experience. "
            "Skeptical / optimistic / mixed; would they come back tomorrow."
        )
    )


# ─── Anthropic client interface (lets tests inject a fake) ────────────────────

class _MessagesClient(Protocol):
    """Subset of the anthropic client we touch. Lets tests pass a fake."""

    def parse(self, **kwargs: Any) -> Any: ...


class _ClientProtocol(Protocol):
    messages: _MessagesClient


def _default_client() -> _ClientProtocol:
    """Lazy-import + instantiate the anthropic client. Raises if the SDK
    isn't installed; never called by the test suite (tests inject)."""
    import anthropic  # type: ignore[import-not-found]
    return anthropic.Anthropic()


# ─── Prompt building (pure, no SDK calls — easy to test) ──────────────────────

def _build_friction_taxonomy(app_config: dict[str, Any]) -> str:
    """The frozen part of the system prompt: friction signal definitions
    from the app config. Same for every persona and session, so it lives
    above the cache_control breakpoint."""
    lines = [
        "You are a UX research analyst reviewing a session log captured by "
        "PersonaLab, a multi-persona QA framework. Your job: extract the "
        "moments where the app failed the persona's mental model, and "
        "classify each by the app's declared friction signal taxonomy.",
        "",
        f"## App under review",
        f"{app_config['app']['name']}: {app_config['app']['description'].strip()}",
        "",
        "## Friction signal taxonomy",
    ]
    for sig in app_config.get("friction_signals", []):
        desc = " ".join(sig.get("description", "").split())  # collapse whitespace
        lines.append(f"- **{sig['type']}** — {desc}")
    lines += [
        "",
        "## Output discipline",
        "- Anchor every friction_event to a concrete event in the session log.",
        "- Use severity 'high' when the persona is blocked, 'medium' when "
        "  they're annoyed but can proceed, 'low' for polish.",
        "- Use signal_type values *only* from the taxonomy above — do not invent.",
        "- Prefer 3-7 friction events over a dump of 20.",
        "- The runner logs 'no matching affordance' reasoning events when the "
        "  persona wanted an action the page didn't expose — these are first-"
        "  class missing_action friction; surface them.",
        "- The runner logs 'intent logged, click suppressed' when an action "
        "  would have mutated protected files; treat the *attempted intent* "
        "  (e.g. 'persona wanted to mark role evaluated') as signal, not the "
        "  suppression itself.",
    ]
    return "\n".join(lines)


def _build_persona_context(persona: dict[str, Any], persona_id: str) -> str:
    """The per-persona part of the system prompt. Cacheable within a run
    of the same persona, but changes between personas."""
    engagement = archetype_engagement(persona)
    behavioral = persona["behavioral"]
    sensitivities = ", ".join(persona.get("friction_sensitivities", [])) or "(none declared)"
    targets = ", ".join(persona.get("target_archetypes", [])) or "(open to all)"
    return "\n".join([
        f"## Persona under simulation: {persona['identity']['name']} ({persona_id})",
        f"- Role: {persona['identity']['role']}",
        f"- Background: {persona['identity']['background'].strip()}",
        f"- Meta-attitude: {persona['meta_attitude']}",
        f"- Target archetypes: {targets}",
        f"- Engagement breadth: {engagement}",
        f"- Comp floor: ${persona['comp_floor']:,}",
        f"- Behavioral: {behavioral['click_speed']} clicker, "
        f"  reads_details={behavioral['reads_details']}, "
        f"  rejection_threshold={behavioral['rejection_threshold']}, "
        f"  detail_dwell_ms={behavioral['detail_dwell_ms']}",
        f"- Especially sensitive to: {sensitivities}",
        "",
        "Weight your friction analysis by this persona's sensitivities. A "
        "scoring_opacity moment matters more to a persona who declared "
        "sensitivity to it.",
    ])


def _build_user_message(events: list[dict[str, Any]]) -> str:
    """The per-call payload: the JSONL session events as a single message.

    Trimmed to the fields the analyzer needs — full ``page_state`` bodies
    can be large and most fields don't drive analysis. We keep titles,
    visible actions, render times, and console errors.
    """
    trimmed: list[dict[str, Any]] = []
    for ev in events:
        e: dict[str, Any] = {
            "ts": ev.get("ts"),
            "event_type": ev.get("event_type"),
            "route": ev.get("route"),
            "action": ev.get("action"),
            "reasoning": ev.get("reasoning"),
            "render_time_ms": ev.get("render_time_ms"),
        }
        ps = ev.get("page_state")
        if isinstance(ps, dict):
            e["page_state"] = {
                "url": ps.get("url"),
                "status": ps.get("status"),
                "title": ps.get("title"),
                "body_text_length": ps.get("body_text_length"),
                "visible_action_names": ps.get("visible_action_names"),
                "console_errors": ps.get("console_errors"),
                "nav_error": ps.get("nav_error"),
                "entered_via": ps.get("entered_via"),
            }
        # Drop keys whose value is None to keep the payload compact.
        trimmed.append({k: v for k, v in e.items() if v is not None})

    return "\n".join([
        "Here is the session log. Return a FrictionReport per the schema.",
        "",
        "```jsonl",
        *[json.dumps(e, ensure_ascii=False) for e in trimmed],
        "```",
    ])


# ─── Markdown rendering (pure) ────────────────────────────────────────────────

def render_markdown(persona_id: str, persona: dict[str, Any], report: FrictionReport) -> str:
    """Human-readable rendering of the structured report."""
    name = persona["identity"]["name"]

    def _sev_sort_key(e: FrictionEvent) -> int:
        return {"high": 0, "medium": 1, "low": 2}.get(e.severity.lower(), 3)

    events_sorted = sorted(report.friction_events, key=_sev_sort_key)

    lines = [
        f"# Friction report — {name}",
        f"_persona_id: `{persona_id}` · generated: {_iso_now()}_",
        "",
        "## Overall verdict",
        report.overall_verdict.strip() or "_(no verdict provided)_",
        "",
        "## Friction events",
    ]
    if not events_sorted:
        lines.append("_None surfaced._")
    for ev in events_sorted:
        lines += [
            f"### [{ev.severity.upper()}] {ev.signal_type} — {ev.location}",
            ev.description.strip(),
            "",
            f"- **Expected:** {ev.what_persona_expected.strip()}",
            f"- **Actually:** {ev.what_actually_happened.strip()}",
            "",
        ]

    if report.ux_issues:
        lines.append("## UX issues")
        for u in report.ux_issues:
            lines.append(f"- _{u.severity}_ · {u.location}: {u.description}")
        lines.append("")

    if report.opportunities:
        lines.append("## Opportunities")
        for o in report.opportunities:
            lines += [
                f"- **{o.description}**",
                f"    _wanted_: {o.what_user_wanted}",
                f"    _blocked by_: {o.why_blocked}",
            ]
        lines.append("")

    if report.wins:
        lines.append("## Wins")
        for w in report.wins:
            lines.append(f"- {w.location}: {w.description}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─── Analyzer ─────────────────────────────────────────────────────────────────

@dataclass
class AnalyzerConfig:
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    reports_dir: Path = field(default_factory=lambda: Path("qa/reports"))


class FrictionAnalyzer:
    """One instance per (app_config, reports_dir). Reusable across personas
    so the prompt cache stays warm.
    """

    def __init__(
        self,
        app_config: dict[str, Any],
        reports_dir: str | Path | None = None,
        client: _ClientProtocol | None = None,
        config: AnalyzerConfig | None = None,
    ) -> None:
        self.app_config = app_config
        self.config = config or AnalyzerConfig()
        if reports_dir is not None:
            self.reports_dir = Path(reports_dir)
        else:
            self.reports_dir = self.config.reports_dir
        self._client = client  # lazy if None
        self._taxonomy = _build_friction_taxonomy(app_config)

    @property
    def client(self) -> _ClientProtocol:
        if self._client is None:
            self._client = _default_client()
        return self._client

    # ─── Prompt construction (testable without SDK) ───────────────────────────

    def build_messages_kwargs(
        self,
        persona: dict[str, Any],
        persona_id: str,
        session_path: str | Path,
    ) -> dict[str, Any]:
        """Construct the full kwargs dict for ``messages.parse``. Useful as
        a seam for tests that want to inspect what we send, and for callers
        who want to dry-run the prompt without hitting the API."""
        events = read_session(session_path)
        persona_context = _build_persona_context(persona, persona_id)
        user_message = _build_user_message(events)

        # Cache the taxonomy block — same across every persona's analysis
        # in a run, so we get ~5 cache reads on a 6-persona orchestrator.
        # The per-persona context is the second block (uncached).
        system_blocks = [
            {
                "type": "text",
                "text": self._taxonomy,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": persona_context,
            },
        ]

        return {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "thinking": {"type": "adaptive"},
            "system": system_blocks,
            "messages": [
                {"role": "user", "content": user_message},
            ],
            "output_format": FrictionReport,
        }

    # ─── Main entrypoint ──────────────────────────────────────────────────────

    def analyze_session(
        self,
        persona: dict[str, Any],
        persona_id: str,
        session_path: str | Path,
    ) -> dict[str, Any]:
        """Call Claude, parse the structured response, write the markdown
        report. Returns a summary dict with paths + counts.
        """
        kwargs = self.build_messages_kwargs(persona, persona_id, session_path)
        response = self.client.messages.parse(**kwargs)
        report = response.parsed_output
        if not isinstance(report, FrictionReport):  # defensive
            raise RuntimeError(
                f"Expected FrictionReport, got {type(report).__name__}"
            )

        self.reports_dir.mkdir(parents=True, exist_ok=True)
        slug = _timestamp_slug()
        md_path = self.reports_dir / f"{persona_id}-{slug}.md"
        json_path = self.reports_dir / f"{persona_id}-{slug}.json"

        md_path.write_text(render_markdown(persona_id, persona, report), encoding="utf-8")
        json_path.write_text(
            json.dumps(report.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        usage = getattr(response, "usage", None)
        return {
            "persona_id": persona_id,
            "session_path": str(session_path),
            "report_md": str(md_path),
            "report_json": str(json_path),
            "friction_event_count": len(report.friction_events),
            "high_severity_count": sum(
                1 for e in report.friction_events if e.severity.lower() == "high"
            ),
            "model": kwargs["model"],
            "usage": _usage_to_dict(usage),
        }


# ─── Small utility helpers ────────────────────────────────────────────────────

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    out = {}
    for k in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = getattr(usage, k, None)
        if v is not None:
            out[k] = v
    return out or None


__all__ = [
    "DEFAULT_MODEL",
    "AnalyzerConfig",
    "FrictionAnalyzer",
    "FrictionEvent",
    "FrictionReport",
    "Opportunity",
    "UxIssue",
    "Win",
    "render_markdown",
]
