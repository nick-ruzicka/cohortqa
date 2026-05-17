"""Pure-logic tests for persona behavioral rules. No Playwright."""

from __future__ import annotations

import pytest

from personalab.core.behavior import (
    CLICK_DELAY_MS,
    PROTECTED_SIDE_EFFECT_PREFIXES,
    actions_for_route,
    archetype_engagement,
    chooses_action,
    click_delay_ms,
    detail_dwell_ms,
    is_protected_action,
)


def _persona(
    *,
    speed: str = "medium",
    reads: bool = True,
    threshold: str = "medium",
    dwell: int = 30000,
    archetypes: list[str] | None = None,
) -> dict:
    return {
        "identity": {"name": "t", "role": "t", "background": "t"},
        "behavioral": {
            "click_speed": speed,
            "reads_details": reads,
            "rejection_threshold": threshold,
            "detail_dwell_ms": dwell,
        },
        "meta_attitude": "test",
        "target_archetypes": archetypes if archetypes is not None else ["a"],
        "location_preferences": [],
        "comp_floor": 100000,
        "friction_sensitivities": [],
    }


# ─── click_delay_ms ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "speed,expected",
    [
        ("slow", 2000),
        ("medium", 1000),
        ("medium-fast", 600),
        ("fast", 300),
    ],
)
def test_click_delay_ms_per_speed(speed, expected):
    assert click_delay_ms(_persona(speed=speed)) == expected


def test_click_delay_ms_unknown_speed_falls_back():
    # Defensive: if validation slipped, we still return a sane default.
    p = _persona()
    p["behavioral"]["click_speed"] = "warp"
    assert click_delay_ms(p) == 1000


# ─── detail_dwell_ms ──────────────────────────────────────────────────────────

def test_detail_dwell_zero_when_not_a_reader():
    assert detail_dwell_ms(_persona(reads=False)) == 0


def test_detail_dwell_passthrough_when_reader():
    assert detail_dwell_ms(_persona(reads=True, dwell=45000)) == 45000


# ─── chooses_action ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "action,expected",
    [
        ("expand_role_row", True),       # reader → expansion: yes
        ("click_role_row", True),        # treated as expansion: yes
        ("open_company_detail", True),   # drilldown for reader: yes
        ("run_scan", True),              # medium speed: yes
        ("mark_skipped", True),          # medium threshold: yes
        ("show_more_roles", True),       # reader OR slow: yes
        ("view_profile", True),          # navigation always: yes
    ],
)
def test_chooses_action_default_persona(action, expected):
    assert chooses_action(_persona(), action) is expected


def test_slow_persona_skips_scan():
    assert chooses_action(_persona(speed="slow"), "run_scan") is False


def test_high_threshold_persona_skips_status_changes():
    p = _persona(threshold="high")
    assert chooses_action(p, "mark_skipped") is False
    assert chooses_action(p, "mark_evaluated") is False


def test_non_reader_skips_expansion_and_drilldown():
    p = _persona(reads=False)
    assert chooses_action(p, "expand_role_row") is False
    assert chooses_action(p, "open_company_detail") is False


# ─── actions_for_route ────────────────────────────────────────────────────────

def test_actions_for_route_preserves_order_and_caps():
    route = {
        "path": "/pipeline",
        "actions": [
            "run_scan",
            "click_role_row",
            "mark_skipped",
            "show_more_roles",
            "open_role_url",  # category=other → not chosen
        ],
    }
    chosen = actions_for_route(_persona(), route, cap=3)
    assert chosen[0] == "run_scan"           # order preserved
    assert chosen[1] == "click_role_row"
    assert chosen[2] == "mark_skipped"
    assert len(chosen) == 3                  # capped


def test_actions_for_route_filters_unwanted():
    route = {
        "path": "/r",
        "actions": ["open_role_url"],        # category=other
    }
    assert actions_for_route(_persona(), route) == []


# ─── archetype_engagement ─────────────────────────────────────────────────────

def test_archetype_engagement_open():
    assert archetype_engagement(_persona(archetypes=[])) == "open"


def test_archetype_engagement_focused():
    assert archetype_engagement(_persona(archetypes=["x"])) == "focused"


def test_archetype_engagement_selective():
    assert archetype_engagement(_persona(archetypes=["x", "y"])) == "selective"


# ─── CLICK_DELAY_MS const integrity ───────────────────────────────────────────

def test_click_delay_ms_keys_match_validator_vocabulary():
    """If we add a click_speed to KNOWN_CLICK_SPEEDS, this test reminds us
    to add a delay for it too."""
    from personalab.core.persona_schema import KNOWN_CLICK_SPEEDS
    assert set(CLICK_DELAY_MS.keys()) == KNOWN_CLICK_SPEEDS


# ─── is_protected_action ──────────────────────────────────────────────────────

def test_is_protected_action_true_for_writes_applications_md():
    """An action whose side_effects list includes any ``writes:<file>`` must
    be flagged so the runner suppresses the click."""
    action = {
        "name": "mark_evaluated",
        "selector": 'button:has-text("Evaluated")',
        "side_effects": ["emits_event:role.status_changed", "writes:applications.md"],
    }
    assert is_protected_action(action) is True


def test_is_protected_action_true_for_any_writes_prefix():
    """Substring matching: any ``writes:`` side-effect protects, regardless
    of file. Future apps may declare other protected files without
    PersonaLab needing an explicit whitelist."""
    action = {"side_effects": ["writes:data/score-overrides.json"]}
    assert is_protected_action(action) is True


def test_is_protected_action_false_when_only_events():
    """``emits_event:*`` is observable but doesn't mutate user state — the
    runner is fine to click these."""
    action = {
        "name": "run_scan",
        "side_effects": ["emits_event:scan.started"],
    }
    assert is_protected_action(action) is False


def test_is_protected_action_false_when_no_side_effects():
    """A bare action (no side_effects key) is not protected — there's
    nothing declared that needs guarding."""
    assert is_protected_action({"name": "filter_signals"}) is False
    assert is_protected_action({"name": "filter_signals", "side_effects": None}) is False
    assert is_protected_action({"name": "filter_signals", "side_effects": []}) is False


def test_is_protected_action_handles_navigation_side_effects():
    """``navigates_to:...`` is not a write; runner should click these so
    detail-route traversal can fire."""
    action = {"side_effects": ["navigates_to:/companies/[slug]"]}
    assert is_protected_action(action) is False


def test_protected_side_effect_prefixes_includes_writes():
    """Constant integrity: anyone reading PROTECTED_SIDE_EFFECT_PREFIXES
    sees ``writes:`` as the canonical protection."""
    assert "writes:" in PROTECTED_SIDE_EFFECT_PREFIXES
