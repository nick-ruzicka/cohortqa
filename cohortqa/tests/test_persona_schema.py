"""Tests for PersonaLab schema validators.

Each fixture in ``fixtures/`` is one configuration. ``valid_*`` should pass;
``invalid_*`` should produce errors. We assert on substrings of the error
message so the test catches the right failure without locking us into exact
phrasing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personalab.core.persona_schema import (
    SchemaError,
    load_app_config,
    load_persona,
    load_scenario,
    validate_app_config,
    validate_persona,
    validate_persona_against_app,
    validate_scenario,
)

import yaml

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return yaml.safe_load((FIXTURES / name).read_text())


# ─── App-config ───────────────────────────────────────────────────────────────

def test_valid_app_passes():
    ok, errors = validate_app_config(_load("valid_app.yaml"))
    assert ok, errors
    assert errors == []


def test_app_missing_keys_reports_each():
    ok, errors = validate_app_config(_load("invalid_app_missing_keys.yaml"))
    assert not ok
    # Should call out each missing top-level key by name.
    msg = "\n".join(errors)
    assert "actions" in msg
    assert "friction_signals" in msg
    assert "personas_dir" in msg


def test_app_unknown_friction_type_named_in_error():
    ok, errors = validate_app_config(_load("invalid_app_bad_friction_type.yaml"))
    assert not ok
    msg = "\n".join(errors)
    assert "wibble" in msg
    assert "friction" in msg


def test_app_route_references_undefined_action():
    ok, errors = validate_app_config(
        _load("invalid_app_route_uses_unknown_action.yaml")
    )
    assert not ok
    msg = "\n".join(errors)
    assert "undefined_action" in msg


def test_app_bad_url_for_dev_server():
    ok, errors = validate_app_config(_load("invalid_app_bad_url.yaml"))
    assert not ok
    msg = "\n".join(errors)
    assert "dev_server" in msg


def test_app_route_path_missing_leading_slash():
    ok, errors = validate_app_config(_load("invalid_app_route_path_no_slash.yaml"))
    assert not ok
    msg = "\n".join(errors)
    assert "must start with '/'" in msg


def test_load_app_config_raises_with_all_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text((FIXTURES / "invalid_app_missing_keys.yaml").read_text())
    with pytest.raises(SchemaError) as exc:
        load_app_config(bad)
    # The exception's .errors list should be non-empty and the message
    # should mention the file path.
    assert exc.value.errors
    assert str(bad) in str(exc.value)


# ─── Persona ──────────────────────────────────────────────────────────────────

def test_valid_persona_passes():
    ok, errors = validate_persona(_load("valid_persona.yaml"))
    assert ok, errors


def test_persona_bad_click_speed_lists_valid_options():
    ok, errors = validate_persona(_load("invalid_persona_bad_click_speed.yaml"))
    assert not ok
    msg = "\n".join(errors)
    assert "click_speed" in msg
    assert "hyperspeed" in msg
    assert "slow" in msg  # the valid-options hint


def test_persona_unknown_sensitivity_named_in_error():
    ok, errors = validate_persona(_load("invalid_persona_unknown_sensitivity.yaml"))
    assert not ok
    msg = "\n".join(errors)
    assert "made_up_signal" in msg


def test_persona_against_app_warns_on_missing_signal():
    # valid_persona declares [navigation, slow_load]; we make an app that only
    # declares navigation, so slow_load is missing.
    persona = _load("valid_persona.yaml")
    app = {
        "friction_signals": [
            {"type": "navigation", "description": "x"},
        ],
    }
    ok, errors = validate_persona_against_app(persona, app)
    assert not ok
    msg = "\n".join(errors)
    assert "slow_load" in msg


def test_persona_against_app_passes_when_all_declared():
    persona = _load("valid_persona.yaml")
    app = _load("valid_app.yaml")  # declares navigation + slow_load
    ok, errors = validate_persona_against_app(persona, app)
    assert ok, errors


def test_load_persona_roundtrip(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text((FIXTURES / "valid_persona.yaml").read_text())
    p = load_persona(f)
    assert p["identity"]["name"] == "Test Persona"


# ─── Scenario ─────────────────────────────────────────────────────────────────

def test_valid_scenario_passes():
    ok, errors = validate_scenario(_load("valid_scenario.yaml"))
    assert ok, errors


def test_scenario_no_modifications_fails():
    ok, errors = validate_scenario(_load("invalid_scenario_no_mods.yaml"))
    assert not ok
    msg = "\n".join(errors)
    assert "at least one of" in msg


def test_scenario_api_mock_response_must_be_json_object_or_list():
    ok, errors = validate_scenario(_load("invalid_scenario_bad_api_mock.yaml"))
    assert not ok
    msg = "\n".join(errors)
    assert "response" in msg


def test_load_scenario_roundtrip(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text((FIXTURES / "valid_scenario.yaml").read_text())
    s = load_scenario(f)
    assert s["name"] == "hide_with_reason"
