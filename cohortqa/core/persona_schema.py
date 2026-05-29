"""Validators for CohortQA's three config types.

Each ``validate_*`` function takes a parsed dict (already YAML-loaded) and
returns ``(ok: bool, errors: list[str])``. Errors are dotted paths plus a
human-readable message — they're meant to land in front of a human, not a
machine, so we prioritise diagnostic clarity over structured error codes.

The convenience ``load_*`` functions read a YAML file from disk, parse it,
validate it, and either return the dict or raise ``SchemaError`` with all
errors joined.

Hand-rolled rather than using ``jsonschema`` because (a) the schemas are
small and stable, (b) keeping the dep surface tiny makes the firewall easy
(``cohortqa/core/*`` is import-clean), and (c) the error messages we
want are domain-specific ("unknown friction signal type 'wibble'") rather
than schema-generic.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml

# ─── Vocabularies ─────────────────────────────────────────────────────────────

# Friction signal types the app-config may declare. Mirrors the comment in
# cohortqa/schemas/app-config.schema.yaml. Apps may use any subset; personas
# may only declare sensitivities to types the app also declares (validated
# cross-config in ``validate_persona_against_app``).
#
# `instrumentation_gap` is the schema escape hatch added in feat/cohortqa-
# improvements for the C6 cascade: when the runner can't tell whether a page
# lacks an affordance or CohortQA's selector is stale, the analyzer must
# have somewhere to file the uncertainty. Without this slot, every
# measurement failure was forced into missing_action/empty_state.
KNOWN_FRICTION_TYPES = frozenset({
    "navigation",
    "scoring_opacity",
    "archetype_confusion",
    "data_density",
    "missing_action",
    "broken_link",
    "slow_load",
    "empty_state",
    "instrumentation_gap",
})

KNOWN_CLICK_SPEEDS = frozenset({"slow", "medium", "medium-fast", "fast"})
KNOWN_REJECTION_THRESHOLDS = frozenset({"low", "medium", "medium-high", "high"})

# Phase B: optional extended-behavior vocabularies. A persona may omit any
# of these fields; the runner uses sensible defaults (mouse / trusting /
# exploratory / low / False) per cohortqa/core/behavior.py.
KNOWN_INPUT_MODALITIES = frozenset({"mouse", "keyboard", "touch"})
KNOWN_TRUST_POSTURES = frozenset({"trusting", "skeptical", "paranoid"})
KNOWN_GOAL_CLARITIES = frozenset({"clear", "exploratory", "lost"})
KNOWN_ERROR_RATES = frozenset({"low", "medium", "high"})

_URL_RE = re.compile(r"^https?://[^\s]+$")


class SchemaError(ValueError):
    """Raised by ``load_*`` helpers when validation fails."""

    def __init__(self, path: str | Path, errors: list[str]):
        self.path = str(path)
        self.errors = errors
        joined = "\n  - ".join(errors)
        super().__init__(f"Schema validation failed for {path}:\n  - {joined}")


# ─── Small typed-field helpers ────────────────────────────────────────────────

def _is_str(v: Any) -> bool:
    return isinstance(v, str) and len(v) > 0


def _is_int(v: Any) -> bool:
    # bool is a subclass of int in Python; reject it explicitly.
    return isinstance(v, int) and not isinstance(v, bool)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_url(v: Any) -> bool:
    return isinstance(v, str) and bool(_URL_RE.match(v))


def _require_keys(d: Any, keys: Iterable[str], where: str, errors: list[str]) -> bool:
    """Append errors for missing keys; return True if all present."""
    if not isinstance(d, dict):
        errors.append(f"{where}: expected a mapping, got {type(d).__name__}")
        return False
    ok = True
    for k in keys:
        if k not in d:
            errors.append(f"{where}.{k}: required key missing")
            ok = False
    return ok


def _require_str(d: dict, key: str, where: str, errors: list[str]) -> None:
    if key in d and not _is_str(d[key]):
        errors.append(f"{where}.{key}: expected non-empty string, got {d[key]!r}")


def _require_int(d: dict, key: str, where: str, errors: list[str]) -> None:
    if key in d and not _is_int(d[key]):
        errors.append(f"{where}.{key}: expected integer, got {d[key]!r}")


def _require_bool(d: dict, key: str, where: str, errors: list[str]) -> None:
    if key in d and not _is_bool(d[key]):
        errors.append(f"{where}.{key}: expected bool, got {d[key]!r}")


def _require_list_of_str(d: dict, key: str, where: str, errors: list[str]) -> None:
    if key in d:
        v = d[key]
        if not isinstance(v, list):
            errors.append(f"{where}.{key}: expected list, got {type(v).__name__}")
            return
        for i, item in enumerate(v):
            if not _is_str(item):
                errors.append(f"{where}.{key}[{i}]: expected string, got {item!r}")


# ─── App-config validator ─────────────────────────────────────────────────────

def validate_app_config(cfg: Any) -> tuple[bool, list[str]]:
    """Validate an app.yaml dict against app-config schema."""
    errors: list[str] = []

    if not _require_keys(
        cfg,
        ["app", "routes", "actions", "friction_signals",
         "personas_dir", "scenarios_dir", "runs_dir"],
        "app-config",
        errors,
    ):
        return (False, errors)

    # app block
    app = cfg.get("app")
    if _require_keys(app, ["name", "dev_server", "description"], "app-config.app", errors):
        _require_str(app, "name", "app-config.app", errors)
        _require_str(app, "description", "app-config.app", errors)
        if "dev_server" in app and not _is_url(app["dev_server"]):
            errors.append(
                f"app-config.app.dev_server: expected http(s) URL, got {app['dev_server']!r}"
            )

    # actions — collect names for cross-refs
    action_names: set[str] = set()
    actions = cfg.get("actions")
    if not isinstance(actions, list):
        errors.append(f"app-config.actions: expected list, got {type(actions).__name__}")
    else:
        for i, action in enumerate(actions):
            where = f"app-config.actions[{i}]"
            if _require_keys(action, ["name", "selector"], where, errors):
                _require_str(action, "name", where, errors)
                _require_str(action, "selector", where, errors)
                if isinstance(action.get("name"), str):
                    action_names.add(action["name"])
            _require_list_of_str(action, "requires", where, errors)
            _require_list_of_str(action, "side_effects", where, errors)

    # routes — each route's actions must reference defined actions
    routes = cfg.get("routes")
    if not isinstance(routes, list) or not routes:
        errors.append("app-config.routes: expected non-empty list")
    else:
        for i, route in enumerate(routes):
            where = f"app-config.routes[{i}]"
            if not _require_keys(route, ["path", "purpose", "actions"], where, errors):
                continue
            _require_str(route, "path", where, errors)
            _require_str(route, "purpose", where, errors)
            _require_int(route, "expected_load_time_ms", where, errors)
            if isinstance(route.get("path"), str) and not route["path"].startswith("/"):
                errors.append(f"{where}.path: must start with '/', got {route['path']!r}")
            _require_list_of_str(route, "actions", where, errors)
            for j, a in enumerate(route.get("actions", []) or []):
                if isinstance(a, str) and action_names and a not in action_names:
                    errors.append(
                        f"{where}.actions[{j}]: unknown action {a!r} "
                        f"(must be defined in app-config.actions)"
                    )

    # friction signals
    signals = cfg.get("friction_signals")
    if not isinstance(signals, list) or not signals:
        errors.append("app-config.friction_signals: expected non-empty list")
    else:
        for i, sig in enumerate(signals):
            where = f"app-config.friction_signals[{i}]"
            if _require_keys(sig, ["type", "description"], where, errors):
                _require_str(sig, "description", where, errors)
                if sig.get("type") not in KNOWN_FRICTION_TYPES:
                    errors.append(
                        f"{where}.type: unknown friction type {sig.get('type')!r}; "
                        f"valid types: {sorted(KNOWN_FRICTION_TYPES)}"
                    )

    # dirs
    for key in ("personas_dir", "scenarios_dir", "runs_dir"):
        _require_str(cfg, key, "app-config", errors)

    return (not errors, errors)


# ─── Persona validator ────────────────────────────────────────────────────────

def validate_persona(cfg: Any) -> tuple[bool, list[str]]:
    """Validate a persona dict against persona schema."""
    errors: list[str] = []

    if not _require_keys(
        cfg,
        ["identity", "target_archetypes", "location_preferences",
         "comp_floor", "behavioral", "meta_attitude", "friction_sensitivities"],
        "persona",
        errors,
    ):
        return (False, errors)

    identity = cfg.get("identity")
    if _require_keys(identity, ["name", "role", "background"], "persona.identity", errors):
        for k in ("name", "role", "background"):
            _require_str(identity, k, "persona.identity", errors)

    _require_list_of_str(cfg, "target_archetypes", "persona", errors)
    _require_list_of_str(cfg, "location_preferences", "persona", errors)
    _require_int(cfg, "comp_floor", "persona", errors)
    _require_str(cfg, "meta_attitude", "persona", errors)
    _require_list_of_str(cfg, "friction_sensitivities", "persona", errors)

    behavioral = cfg.get("behavioral")
    if _require_keys(
        behavioral,
        ["click_speed", "reads_details", "rejection_threshold", "detail_dwell_ms"],
        "persona.behavioral",
        errors,
    ):
        if behavioral.get("click_speed") not in KNOWN_CLICK_SPEEDS:
            errors.append(
                f"persona.behavioral.click_speed: must be one of "
                f"{sorted(KNOWN_CLICK_SPEEDS)}, got {behavioral.get('click_speed')!r}"
            )
        if behavioral.get("rejection_threshold") not in KNOWN_REJECTION_THRESHOLDS:
            errors.append(
                f"persona.behavioral.rejection_threshold: must be one of "
                f"{sorted(KNOWN_REJECTION_THRESHOLDS)}, "
                f"got {behavioral.get('rejection_threshold')!r}"
            )
        _require_bool(behavioral, "reads_details", "persona.behavioral", errors)
        _require_int(behavioral, "detail_dwell_ms", "persona.behavioral", errors)

        # Phase B: optional extended-behavior fields. If present, must be
        # from the known vocab; if absent, the runner uses defaults.
        _opt_vocab_field = lambda key, vocab: (
            behavioral.get(key) is not None
            and behavioral.get(key) not in vocab
        )
        if _opt_vocab_field("input_modality", KNOWN_INPUT_MODALITIES):
            errors.append(
                f"persona.behavioral.input_modality: must be one of "
                f"{sorted(KNOWN_INPUT_MODALITIES)}, got "
                f"{behavioral.get('input_modality')!r}"
            )
        if _opt_vocab_field("trust_posture", KNOWN_TRUST_POSTURES):
            errors.append(
                f"persona.behavioral.trust_posture: must be one of "
                f"{sorted(KNOWN_TRUST_POSTURES)}, got "
                f"{behavioral.get('trust_posture')!r}"
            )
        if _opt_vocab_field("goal_clarity", KNOWN_GOAL_CLARITIES):
            errors.append(
                f"persona.behavioral.goal_clarity: must be one of "
                f"{sorted(KNOWN_GOAL_CLARITIES)}, got "
                f"{behavioral.get('goal_clarity')!r}"
            )
        if _opt_vocab_field("error_rate", KNOWN_ERROR_RATES):
            errors.append(
                f"persona.behavioral.error_rate: must be one of "
                f"{sorted(KNOWN_ERROR_RATES)}, got "
                f"{behavioral.get('error_rate')!r}"
            )
        if "has_prior_session" in behavioral and not isinstance(
            behavioral["has_prior_session"], bool
        ):
            errors.append(
                f"persona.behavioral.has_prior_session: must be bool, got "
                f"{type(behavioral['has_prior_session']).__name__}"
            )

    # Friction sensitivities must be from the known vocabulary; the
    # cross-config check (against an app's declared signals) is separate.
    for i, s in enumerate(cfg.get("friction_sensitivities", []) or []):
        if isinstance(s, str) and s not in KNOWN_FRICTION_TYPES:
            errors.append(
                f"persona.friction_sensitivities[{i}]: unknown type {s!r}; "
                f"valid types: {sorted(KNOWN_FRICTION_TYPES)}"
            )

    return (not errors, errors)


# ─── Scenario validator ───────────────────────────────────────────────────────

def validate_scenario(cfg: Any) -> tuple[bool, list[str]]:
    """Validate a scenario dict against scenario schema."""
    errors: list[str] = []

    if not _require_keys(
        cfg, ["name", "description", "based_on", "modifications"], "scenario", errors
    ):
        return (False, errors)

    _require_str(cfg, "name", "scenario", errors)
    _require_str(cfg, "description", "scenario", errors)
    _require_str(cfg, "based_on", "scenario", errors)

    mods = cfg.get("modifications")
    if not isinstance(mods, dict):
        errors.append(
            f"scenario.modifications: expected mapping, got {type(mods).__name__}"
        )
        return (not errors, errors)

    # At least one modification kind must be present and non-empty.
    has_any_mod = False
    for kind in ("dom_injections", "api_mocks", "config_overrides"):
        if kind in mods and mods[kind]:
            has_any_mod = True
    if not has_any_mod:
        errors.append(
            "scenario.modifications: at least one of dom_injections, "
            "api_mocks, config_overrides must be present and non-empty"
        )

    # dom_injections
    for i, inj in enumerate(mods.get("dom_injections", []) or []):
        where = f"scenario.modifications.dom_injections[{i}]"
        if _require_keys(inj, ["route", "selector", "html"], where, errors):
            for k in ("route", "selector", "html"):
                _require_str(inj, k, where, errors)

    # api_mocks
    for i, mock in enumerate(mods.get("api_mocks", []) or []):
        where = f"scenario.modifications.api_mocks[{i}]"
        if _require_keys(mock, ["endpoint", "response"], where, errors):
            _require_str(mock, "endpoint", where, errors)
            if "response" in mock and not isinstance(mock["response"], (dict, list)):
                errors.append(
                    f"{where}.response: expected JSON object or list, "
                    f"got {type(mock['response']).__name__}"
                )

    # config_overrides
    for i, ov in enumerate(mods.get("config_overrides", []) or []):
        where = f"scenario.modifications.config_overrides[{i}]"
        if _require_keys(ov, ["key", "value"], where, errors):
            _require_str(ov, "key", where, errors)
            # `value` is intentionally untyped — overrides are arbitrary.

    return (not errors, errors)


# ─── Cross-config check ───────────────────────────────────────────────────────

def validate_persona_against_app(
    persona: dict, app_config: dict
) -> tuple[bool, list[str]]:
    """Warn when a persona's friction_sensitivities reference signal types
    the app doesn't declare. Soft-fail: returns errors but the caller decides
    whether to treat as fatal."""
    errors: list[str] = []
    app_types = {s.get("type") for s in app_config.get("friction_signals", []) if isinstance(s, dict)}
    for i, sens in enumerate(persona.get("friction_sensitivities", []) or []):
        if sens not in app_types:
            errors.append(
                f"persona.friction_sensitivities[{i}]: type {sens!r} not declared in "
                f"app-config.friction_signals (declared: {sorted(app_types)})"
            )
    return (not errors, errors)


# ─── Loaders ──────────────────────────────────────────────────────────────────

def _load_yaml(path: str | Path) -> Any:
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text)


def load_app_config(path: str | Path) -> dict:
    cfg = _load_yaml(path)
    ok, errors = validate_app_config(cfg)
    if not ok:
        raise SchemaError(path, errors)
    return cfg


def load_persona(path: str | Path) -> dict:
    cfg = _load_yaml(path)
    ok, errors = validate_persona(cfg)
    if not ok:
        raise SchemaError(path, errors)
    return cfg


def load_scenario(path: str | Path) -> dict:
    cfg = _load_yaml(path)
    ok, errors = validate_scenario(cfg)
    if not ok:
        raise SchemaError(path, errors)
    return cfg


__all__ = [
    "KNOWN_FRICTION_TYPES",
    "KNOWN_CLICK_SPEEDS",
    "KNOWN_REJECTION_THRESHOLDS",
    "SchemaError",
    "validate_app_config",
    "validate_persona",
    "validate_scenario",
    "validate_persona_against_app",
    "load_app_config",
    "load_persona",
    "load_scenario",
]
