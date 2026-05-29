"""Pure behavioral rules for personas. No Playwright import; safe to call
from anywhere, fast to unit-test.

The runner uses these to decide which actions a persona would take on a
route, plus the timing between actions. Decisions are deterministic so
session logs replay byte-for-byte across runs (see ``replayer.py``,
planned P6). Stochasticity, if we ever want it, belongs in the
analyzer — Claude can roll the dice on rationale.

Phase B added five optional persona fields that change what the runner
physically does, not just what the analyzer reads. All five default to
the Phase A behavior (mouse/trusting/exploratory/low/false) — old personas
keep navigating exactly as before, while new personas can opt into
keyboard navigation, error simulation, route filtering, etc.
"""

from __future__ import annotations

from typing import Any

# ─── Phase B: extended-behavior defaults ─────────────────────────────────────
#
# Optional persona fields, with defaults that match Phase A behavior
# (mouse, trusting, exploratory, low error, no prior session). The runner
# consults these via the small accessor helpers below so reaching into
# the dict in multiple places doesn't drift.

_DEFAULT_INPUT_MODALITY = "mouse"          # mouse | keyboard | touch
_DEFAULT_TRUST_POSTURE = "trusting"        # trusting | skeptical | paranoid
_DEFAULT_GOAL_CLARITY = "exploratory"      # clear | exploratory | lost
                                           # 'exploratory' = Phase A behavior
                                           # (visit every declared route in
                                           # order). Legacy personas without
                                           # a goal_clarity field MUST get
                                           # this default for back-compat.
_DEFAULT_ERROR_RATE = "low"                # low | medium | high
_DEFAULT_HAS_PRIOR_SESSION = False         # bool

KNOWN_INPUT_MODALITIES = frozenset({"mouse", "keyboard", "touch"})
KNOWN_TRUST_POSTURES = frozenset({"trusting", "skeptical", "paranoid"})
KNOWN_GOAL_CLARITIES = frozenset({"clear", "exploratory", "lost"})
KNOWN_ERROR_RATES = frozenset({"low", "medium", "high"})


def input_modality(persona: dict[str, Any]) -> str:
    """The persona's input modality — drives whether the runner uses
    locator.click() (mouse), locator.tap() (touch), or focus+keyboard.press
    ("Enter") (keyboard). Defaults to mouse for Phase A personas that don't
    declare the field."""
    return (persona.get("behavioral") or {}).get(
        "input_modality", _DEFAULT_INPUT_MODALITY
    )


def trust_posture(persona: dict[str, Any]) -> str:
    """The persona's data-trust posture. Used by the runner to filter
    actions whose declared side_effects look like data-asks (paranoid)
    or signup/persist (paranoid only). Analyzer reads it from the persona
    context for lens weighting."""
    return (persona.get("behavioral") or {}).get(
        "trust_posture", _DEFAULT_TRUST_POSTURE
    )


def goal_clarity(persona: dict[str, Any]) -> str:
    """Goal clarity. 'clear' personas visit routes in declared order and
    stop after 3; 'exploratory' visit every route the app declares (Phase
    A behavior); 'lost' personas re-visit the entry route after each detour
    (modelling a user who keeps returning to home looking for the right
    path)."""
    return (persona.get("behavioral") or {}).get(
        "goal_clarity", _DEFAULT_GOAL_CLARITY
    )


def error_rate(persona: dict[str, Any]) -> str:
    """How often the persona generates noisy interactions (double-clicks,
    browser-back, mis-targeted clicks). 'low' → never; 'medium' → on
    every ~3rd action; 'high' → on every ~2nd action."""
    return (persona.get("behavioral") or {}).get(
        "error_rate", _DEFAULT_ERROR_RATE
    )


def has_prior_session(persona: dict[str, Any]) -> bool:
    """True if the persona is a 'returning user' — has muscle memory,
    expects to jump to deep routes without re-walking onboarding. The
    runner uses this to reverse the route visit order (deep first)."""
    return bool((persona.get("behavioral") or {}).get(
        "has_prior_session", _DEFAULT_HAS_PRIOR_SESSION
    ))


def routes_for_persona(
    persona: dict[str, Any],
    routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ordered list of routes the persona will visit.

    - has_prior_session=True reverses the route order (returning users
      jump to deep routes first, treating the landing page as already-
      known).
    - goal_clarity='clear' caps the route count at 3 (focused user heads
      to their known destinations and stops).
    - goal_clarity='lost' interleaves the entry route between each visit
      (modelling a user who returns to home each time looking for the
      right path).
    - goal_clarity='exploratory' (default) visits every declared route
      in order — current Phase A behavior.

    Backward compatibility: a persona with no extended fields gets the
    full ordered route list, identical to Phase A.
    """
    if not routes:
        return []
    seq = list(routes)
    if has_prior_session(persona):
        seq = list(reversed(seq))
    clarity = goal_clarity(persona)
    if clarity == "clear":
        return seq[:3]
    if clarity == "lost":
        entry = seq[0]
        out = [entry]
        for r in seq[1:]:
            out.append(r)
            out.append(entry)  # bounce back to entry between detours
        return out
    # 'exploratory' (default) — full ordered list
    return seq


def should_double_click_after(persona: dict[str, Any], action_index: int) -> bool:
    """Phase B: occasional error-simulation. The error-prone persona
    double-clicks deterministically on the action index (so traces
    replay byte-for-byte)."""
    rate = error_rate(persona)
    if rate == "high":
        return action_index % 2 == 0
    if rate == "medium":
        return action_index % 3 == 0
    return False


def should_go_back_after(persona: dict[str, Any], action_index: int) -> bool:
    """Phase B: simulate a user who occasionally hits browser back —
    error-prone or lost personas. Deterministic on action_index."""
    if goal_clarity(persona) == "lost":
        # Lost personas hit back periodically because they realise they're
        # in the wrong place.
        return action_index > 0 and action_index % 3 == 0
    if error_rate(persona) == "high":
        return action_index > 0 and action_index % 4 == 0
    return False


def trust_filters_action(persona: dict[str, Any], action: dict[str, Any]) -> bool:
    """Phase B: returns True if the persona's trust posture would refuse
    this action. A paranoid persona skips any action whose side_effects
    advertise data-asks (e.g. ``asks:email``, ``persists:profile``);
    a skeptical persona allows persistence but skips signup/login flows
    that ask for data. Trusting (default) allows everything.

    Action authors signal trust-relevant intent via these prefixes in
    side_effects:
        asks:<field>     — collects a piece of personal data
        signup:<thing>   — creates an account / commits identity
        persists:<thing> — writes data the user owns to a server

    This filter runs BEFORE protected-action enforcement, so an action
    that's both protected and trust-filtered is still recorded as
    intent (the protected-action path), just with an additional
    trust-posture reason."""
    posture = trust_posture(persona)
    if posture == "trusting":
        return False
    sides = action.get("side_effects") or []
    for se in sides:
        if not isinstance(se, str):
            continue
        if posture in {"skeptical", "paranoid"} and se.startswith("asks:"):
            return True
        if posture == "paranoid" and (
            se.startswith("signup:") or se.startswith("persists:")
        ):
            return True
    return False

# ─── Protected side-effects ───────────────────────────────────────────────────
#
# CohortQA must not mutate user-owned files. An action whose
# ``side_effects`` list contains any of these strings is logged as
# *intent* (so the analyzer sees the persona considered it) but the click
# is never actually dispatched. The source-tag on events also lets
# real analytics filter CohortQA traffic out — this constant is the
# defence for things that don't read the source tag (e.g. raw file
# writers).
#
# Matching is substring-based on the side-effect entry so an app config
# like ``writes:applications.md`` or ``writes:data/score-overrides.json``
# both protect correctly without ceremony.
PROTECTED_SIDE_EFFECT_PREFIXES: tuple[str, ...] = ("writes:",)


def is_protected_action(action: dict[str, Any]) -> bool:
    """Whether the runner should skip *executing* this action.

    Returns True when any declared side-effect would mutate a protected
    file (anything prefixed ``writes:``). The runner still logs the
    persona's intent so the analyzer can surface "this persona wanted
    to mark X as evaluated" without actually doing it.
    """
    for se in action.get("side_effects") or []:
        if isinstance(se, str) and se.startswith(PROTECTED_SIDE_EFFECT_PREFIXES):
            return True
    return False

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
    "PROTECTED_SIDE_EFFECT_PREFIXES",
    "KNOWN_INPUT_MODALITIES",
    "KNOWN_TRUST_POSTURES",
    "KNOWN_GOAL_CLARITIES",
    "KNOWN_ERROR_RATES",
    "click_delay_ms",
    "detail_dwell_ms",
    "chooses_action",
    "actions_for_route",
    "archetype_engagement",
    "is_protected_action",
    # Phase B
    "input_modality",
    "trust_posture",
    "goal_clarity",
    "error_rate",
    "has_prior_session",
    "routes_for_persona",
    "should_double_click_after",
    "should_go_back_after",
    "trust_filters_action",
]
