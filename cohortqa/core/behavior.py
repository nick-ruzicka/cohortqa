"""Pure behavioral rules for personas. No Playwright import; safe to call
from anywhere, fast to unit-test.

The runner uses these to decide which actions a persona would take on a
route, plus the timing between actions. Decisions are deterministic so
session logs replay byte-for-byte across runs (see ``replayer.py``,
planned P6). Stochasticity, if we ever want it, belongs in the
analyzer — Claude can roll the dice on rationale.
"""

from __future__ import annotations

from typing import Any

# Wall-clock-ish delays between successive actions, by click_speed.
# These are the inter-action "thinking" pause. Render waits stack on top
# of these in the runner.
CLICK_DELAY_MS: dict[str, int] = {
    "slow": 2000,
    "medium": 1000,
    "medium-fast": 600,
    "fast": 300,
}


def click_delay_ms(persona: dict[str, Any]) -> int:
    """How long the persona pauses between actions."""
    speed = persona["behavioral"]["click_speed"]
    return CLICK_DELAY_MS.get(speed, 1000)


def detail_dwell_ms(persona: dict[str, Any]) -> int:
    """How long the persona stays on a detail panel they opened. 0 if they
    don't read details."""
    behavioral = persona["behavioral"]
    return behavioral["detail_dwell_ms"] if behavioral["reads_details"] else 0


def _action_category(name: str) -> str:
    """Classify an action name by intent. Categories drive the chooses_action
    rules below; matching is substring-based so app-config can stay
    free-form."""
    n = name.lower()
    if "scan" in n:
        return "scan"
    if name in {"expand_role_row", "click_role_row"} or "expand" in n:
        return "expansion"
    # ``open_<thing>`` is treated as drilldown only when it stays in-app.
    # ``open_role_url`` and friends route the user to an external page and
    # would derail the session — leave those to the human.
    if n.startswith("open_") and "_url" not in n and "external" not in n:
        return "drilldown"
    if "drilldown" in n:
        return "drilldown"
    if "mark_" in n or "_status" in n:
        return "status_change"
    if "filter" in n or "show_more" in n or "load_more" in n:
        return "pagination_or_filter"
    if "back" in n or n.startswith("view_"):
        return "navigation"
    return "other"


def chooses_action(persona: dict[str, Any], action_name: str) -> bool:
    """Would this persona take this action on first visit to a route?

    Rules are deterministic and behavior-driven:

    * Detail-readers expand and drill into detail panels.
    * All but slow personas trigger fresh scans (slow personas don't want
      to wait through one).
    * Low/medium rejection-threshold personas try status-change actions
      (skipping liberally); high-threshold personas don't bother — they're
      patient with the default view.
    * Filter/pagination actions are for detail-readers and slow personas.
    * Navigation/back/view actions are taken by all personas when present.
    """
    cat = _action_category(action_name)
    speed = persona["behavioral"]["click_speed"]
    reads = persona["behavioral"]["reads_details"]
    threshold = persona["behavioral"]["rejection_threshold"]

    if cat == "expansion":
        return reads
    if cat == "drilldown":
        return reads
    if cat == "scan":
        return speed != "slow"
    if cat == "status_change":
        return threshold in {"low", "medium"}
    if cat == "pagination_or_filter":
        return reads or speed == "slow"
    if cat == "navigation":
        return True
    return False


def actions_for_route(
    persona: dict[str, Any],
    route: dict[str, Any],
    cap: int = 3,
) -> list[str]:
    """The ordered list of action names a persona would take on a route.

    Caps at ``cap`` actions per route so a single page doesn't dominate the
    session. The order matches ``route["actions"]`` so app-config authors
    can express precedence (put the primary CTA first).
    """
    return [a for a in route["actions"] if chooses_action(persona, a)][:cap]


def archetype_engagement(persona: dict[str, Any]) -> str:
    """A one-word summary of how broadly the persona engages with the app's
    archetype taxonomy. Used by the analyzer to weight friction reports."""
    targets = persona.get("target_archetypes", [])
    if not targets:
        return "open"          # exploring everything
    if len(targets) == 1:
        return "focused"       # one archetype only
    return "selective"         # multiple but not all


__all__ = [
    "CLICK_DELAY_MS",
    "click_delay_ms",
    "detail_dwell_ms",
    "chooses_action",
    "actions_for_route",
    "archetype_engagement",
]
