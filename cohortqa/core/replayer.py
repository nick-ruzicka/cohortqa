"""Session replayer — backtest a past JSONL session against current code.

Reads the events from a previously-captured session, walks the same route
sequence in the current app, and writes a regression report listing
every drift: missing routes, render times outside a ±50% tolerance band,
visible-action mismatches, and new console errors.

Use case: "after company drilldown ships, replay senior-gtm-eng's
previous session and confirm nothing broke."

This module re-implements the navigate-and-capture primitives locally
instead of importing them from ``runner.py``. The runner's logic is
behavioral (persona decides what to do); the replayer is *scripted*
(events.jsonl decides). Keeping the two paths independent makes it
clear that a replay won't accidentally re-run a persona's
mark-evaluated intent or hit a protected file.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .runner import read_session


# Same hook signature as PersonaRunner's — lets the smoke test inject
# Playwright route interception without a real dev server.
ContextHook = Callable[[Any], Awaitable[None]]

# Render time tolerance: the spec says ±50%. Below the floor we consider
# any tiny absolute difference acceptable so a 12ms→24ms swing doesn't
# trip the regression alarm.
RENDER_TOLERANCE_PCT = 0.50
RENDER_TOLERANCE_FLOOR_MS = 100


# ─── Plan extraction (pure — no Playwright) ──────────────────────────────────

@dataclass
class ReplayStep:
    """One route in the replay plan, with the actions to re-issue."""
    route: str
    expected_render_ms: int
    expected_visible_actions: list[str]
    expected_console_errors: int
    actions_to_take: list[dict[str, Any]]  # [{name, selector}]


def build_replay_plan(
    events: list[dict[str, Any]],
    app_config: dict[str, Any] | None = None,
) -> list[ReplayStep]:
    """Group session events into per-route replay steps.

    Each ``nav`` event starts a new step; subsequent ``action`` events
    belong to that step until the next ``nav``. Detail-route navs (which
    the runner records with ``render_time_ms=0`` and ``entered_via=
    detail_route_traversal``) are folded into the previous step's
    actions list so the replayer doesn't try to re-navigate them
    independently.
    """
    plan: list[ReplayStep] = []
    current: ReplayStep | None = None
    actions_by_name: dict[str, dict[str, Any]] = (
        {a["name"]: a for a in (app_config or {}).get("actions", [])}
        if app_config else {}
    )

    for ev in events:
        et = ev.get("event_type")
        if et == "nav":
            ps = ev.get("page_state") or {}
            entered_via = ps.get("entered_via")
            if entered_via == "detail_route_traversal" and current is not None:
                # Don't start a new step — detail routes are navigated by
                # a prior action, not directly. We do still track the
                # expected console error count via the parent step.
                continue
            current = ReplayStep(
                route=ev.get("route") or "",
                expected_render_ms=ev.get("render_time_ms") or 0,
                expected_visible_actions=list(ps.get("visible_action_names") or []),
                expected_console_errors=len(ps.get("console_errors") or []),
                actions_to_take=[],
            )
            plan.append(current)
        elif et == "action" and current is not None:
            name = ev.get("action")
            spec = actions_by_name.get(name) if name else None
            current.actions_to_take.append({
                "name": name,
                "selector": (spec or {}).get("selector") or ev.get("selector"),
            })
    return plan


# ─── Replay execution ────────────────────────────────────────────────────────

@dataclass
class StepResult:
    route: str
    nav_succeeded: bool
    render_ms: int
    visible_actions: list[str]
    console_errors: int
    nav_error: str | None
    action_results: list[dict[str, Any]] = field(default_factory=list)


class SessionReplayer:
    """Drive Playwright through a recorded session's route + action sequence."""

    def __init__(
        self,
        app_config: dict[str, Any],
        original_session_path: str | Path,
        regressions_dir: str | Path,
        dev_server_override: str | None = None,
        headless: bool = True,
        context_hook: ContextHook | None = None,
    ) -> None:
        self.app_config = app_config
        self.original_session_path = Path(original_session_path)
        self.regressions_dir = Path(regressions_dir)
        self.dev_server = dev_server_override or app_config["app"]["dev_server"]
        self.headless = headless
        self.context_hook = context_hook
        self.console_errors: list[str] = []

    async def replay(self) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        original_events = read_session(self.original_session_path)
        plan = build_replay_plan(original_events, self.app_config)
        observed: list[StepResult] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            try:
                context = await browser.new_context(
                    user_agent="PersonaLab/0.1 (replayer)",
                    viewport={"width": 1440, "height": 900},
                )
                if self.context_hook:
                    await self.context_hook(context)
                page = await context.new_page()
                page.on("console", lambda msg: self._on_console(msg))
                page.on("pageerror", lambda err: self.console_errors.append(str(err)))

                for step in plan:
                    observed.append(await self._replay_step(page, step))

                await context.close()
            finally:
                await browser.close()

        diff = compute_replay_diff(plan, observed)
        report_path = self._write_report(plan, observed, diff)
        return {
            "original_session": str(self.original_session_path),
            "report_path": str(report_path),
            "diff": diff,
            "steps_replayed": len(observed),
        }

    async def _replay_step(self, page: Any, step: ReplayStep) -> StepResult:
        url = self.dev_server.rstrip("/") + step.route
        before = time.monotonic()
        nav_error: str | None = None
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=10000)
        except Exception as exc:  # noqa: BLE001
            nav_error = repr(exc)
        render_ms = int((time.monotonic() - before) * 1000)

        visible: list[str] = []
        if not nav_error:
            try:
                visible = await _visible_action_names_local(
                    page,
                    step.expected_visible_actions
                    + [a["name"] for a in step.actions_to_take if a.get("name")],
                    self.app_config["actions"],
                )
            except Exception:  # noqa: BLE001
                pass

        result = StepResult(
            route=step.route,
            nav_succeeded=nav_error is None,
            render_ms=render_ms,
            visible_actions=visible,
            console_errors=len(self.console_errors),
            nav_error=nav_error,
        )

        if nav_error:
            return result

        for action in step.actions_to_take:
            selector = action.get("selector")
            if not selector:
                result.action_results.append({
                    "name": action.get("name"),
                    "ok": False,
                    "error": "no selector available — action not in app_config",
                })
                continue
            try:
                locator = page.locator(selector).first
                await locator.click(timeout=2000)
                result.action_results.append({
                    "name": action.get("name"), "ok": True, "error": None,
                })
            except Exception as exc:  # noqa: BLE001
                result.action_results.append({
                    "name": action.get("name"),
                    "ok": False,
                    "error": repr(exc),
                })

        return result

    def _on_console(self, msg: Any) -> None:
        try:
            if msg.type in ("error", "warning"):
                self.console_errors.append(f"[{msg.type}] {msg.text}")
        except Exception:  # noqa: BLE001
            pass

    def _write_report(
        self,
        plan: list[ReplayStep],
        observed: list[StepResult],
        diff: dict[str, Any],
    ) -> Path:
        self.regressions_dir.mkdir(parents=True, exist_ok=True)
        stem = self.original_session_path.stem
        out = self.regressions_dir / f"{stem}-vs-current-{_timestamp_slug()}.md"
        out.write_text(
            render_replay_markdown(
                self.original_session_path, plan, observed, diff
            ),
            encoding="utf-8",
        )
        return out


# ─── Diff (pure) ─────────────────────────────────────────────────────────────

def compute_replay_diff(
    plan: list[ReplayStep],
    observed: list[StepResult],
) -> dict[str, Any]:
    """Compare expected vs observed. Returns per-step + summary."""
    per_step: list[dict[str, Any]] = []
    summary = {
        "total_steps": len(plan),
        "nav_failures": 0,
        "render_regressions": 0,
        "visible_action_mismatches": 0,
        "new_console_errors": 0,
        "action_failures": 0,
        "ok": True,
    }

    for step, obs in zip(plan, observed):
        s: dict[str, Any] = {
            "route": step.route,
            "nav_ok": obs.nav_succeeded,
            "nav_error": obs.nav_error,
            "render_expected_ms": step.expected_render_ms,
            "render_actual_ms": obs.render_ms,
            "render_regression": _render_regressed(step.expected_render_ms, obs.render_ms),
            "missing_actions": sorted(set(step.expected_visible_actions) - set(obs.visible_actions)),
            "new_actions": sorted(set(obs.visible_actions) - set(step.expected_visible_actions)),
            "new_console_errors": max(0, obs.console_errors - step.expected_console_errors),
            "action_results": obs.action_results,
        }
        if not obs.nav_succeeded:
            summary["nav_failures"] += 1
            summary["ok"] = False
        if s["render_regression"]:
            summary["render_regressions"] += 1
            summary["ok"] = False
        if s["missing_actions"] or s["new_actions"]:
            summary["visible_action_mismatches"] += 1
            # Note: only missing actions break things — new actions are
            # additive and don't fail the run.
            if s["missing_actions"]:
                summary["ok"] = False
        if s["new_console_errors"]:
            summary["new_console_errors"] += s["new_console_errors"]
            summary["ok"] = False
        for ar in obs.action_results:
            if not ar.get("ok"):
                summary["action_failures"] += 1
                summary["ok"] = False
        per_step.append(s)

    return {"summary": summary, "steps": per_step}


def _render_regressed(expected: int, actual: int) -> bool:
    """Render-time regression if actual > expected × (1 + tolerance), and
    the absolute difference is above the noise floor."""
    if expected <= 0:
        return False
    if actual - expected <= RENDER_TOLERANCE_FLOOR_MS:
        return False
    return actual > expected * (1 + RENDER_TOLERANCE_PCT)


# ─── Markdown ────────────────────────────────────────────────────────────────

def render_replay_markdown(
    original_session_path: Path,
    plan: list[ReplayStep],
    observed: list[StepResult],
    diff: dict[str, Any],
) -> str:
    summary = diff["summary"]
    status_emoji = "✅" if summary["ok"] else "🚨"

    lines = [
        f"# Replay vs current — {original_session_path.name}",
        f"_generated: {_iso_now()} · status: {status_emoji}_",
        "",
        "## Summary",
        f"- Steps replayed: {summary['total_steps']}",
        f"- Navigation failures: {summary['nav_failures']}",
        f"- Render regressions (>{int(RENDER_TOLERANCE_PCT * 100)}% slower): "
        f"{summary['render_regressions']}",
        f"- Visible-action mismatches: {summary['visible_action_mismatches']}",
        f"- New console errors: {summary['new_console_errors']}",
        f"- Action failures: {summary['action_failures']}",
        "",
        "## Per-step results",
    ]
    for s in diff["steps"]:
        line = f"### `{s['route']}` — "
        if not s["nav_ok"]:
            line += f"🚨 nav failed: {s['nav_error']}"
            lines.append(line)
            continue
        statuses = []
        statuses.append(
            f"render {s['render_actual_ms']}ms vs expected "
            f"{s['render_expected_ms']}ms"
            + (" 🚨" if s["render_regression"] else "")
        )
        if s["missing_actions"]:
            statuses.append(f"missing: {s['missing_actions']} 🚨")
        if s["new_actions"]:
            statuses.append(f"new (ok): {s['new_actions']}")
        if s["new_console_errors"]:
            statuses.append(f"+{s['new_console_errors']} console errors 🚨")
        line += "; ".join(statuses) or "no drift"
        lines.append(line)
        if s["action_results"]:
            for ar in s["action_results"]:
                ok_mark = "✅" if ar["ok"] else "🚨"
                lines.append(
                    f"  - {ok_mark} {ar['name']}"
                    + (f" — {ar['error']}" if ar.get("error") else "")
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _visible_action_names_local(
    page: Any,
    action_names: list[str],
    all_actions: list[dict[str, Any]],
) -> list[str]:
    """Like runner._visible_action_names, but reimplemented locally to
    keep the replayer's import surface minimal (no behavioral coupling)."""
    by_name = {a["name"]: a for a in all_actions}
    seen: list[str] = []
    for name in dict.fromkeys(action_names):  # preserve order + dedupe
        spec = by_name.get(name)
        if not spec:
            continue
        try:
            count = await page.locator(spec["selector"]).count()
            if count > 0:
                seen.append(name)
        except Exception:  # noqa: BLE001
            pass
    return seen


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "RENDER_TOLERANCE_PCT",
    "RENDER_TOLERANCE_FLOOR_MS",
    "ReplayStep",
    "StepResult",
    "SessionReplayer",
    "build_replay_plan",
    "compute_replay_diff",
    "render_replay_markdown",
]
