"""Generic PersonaLab browser runner.

Drives one persona through one app's routes in an isolated headless
Playwright context. Persists a JSONL session log to the app's
``runs_dir``. The runner has no app-specific knowledge — it consumes
``app_config`` (loaded via personalab.core.persona_schema) and the
persona dict, then takes a small handful of behaviorally-justified
actions per route.

The runner *observes* and *interacts*; it doesn't *interpret*. Friction
analysis happens later in ``personalab.core.analyzer`` once the session
log is complete.

Output schema (one JSON object per line in the JSONL):

  {
    "ts": "2026-05-17T07:02:15.123Z",
    "persona_id": "senior-gtm-eng-nyc",
    "source": "personalab:senior-gtm-eng-nyc",
    "event_type": "nav" | "capture" | "action" | "reasoning" | "error" | "scenario_applied",
    "route": "/pipeline" | null,
    "action": "run_scan" | null,
    "selector": "button:has-text('Run Scan')" | null,
    "reasoning": "Medium-speed reader; triggering scan to surface fresh roles." | null,
    "render_time_ms": 1843 | null,
    "page_state": {
      "url": "...",
      "title": "...",
      "body_text_length": 12345,
      "visible_action_names": ["run_scan", "click_role_row"],
      "console_errors": [],
    } | null
  }
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .behavior import (
    actions_for_route,
    archetype_engagement,
    click_delay_ms,
    detail_dwell_ms,
    is_protected_action,
)


# ─── Event model ──────────────────────────────────────────────────────────────

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class SessionEvent:
    persona_id: str
    event_type: str
    route: str | None = None
    action: str | None = None
    selector: str | None = None
    reasoning: str | None = None
    render_time_ms: int | None = None
    page_state: dict[str, Any] | None = None
    ts: str = field(default_factory=_iso_now)

    def to_jsonl(self) -> str:
        # Source tag is derived, not stored — it's the same for every event
        # in a session and we want personalab's events to be filterable in
        # downstream analytics with a single string match.
        d = {
            "ts": self.ts,
            "persona_id": self.persona_id,
            "source": f"personalab:{self.persona_id}",
            "event_type": self.event_type,
            "route": self.route,
            "action": self.action,
            "selector": self.selector,
            "reasoning": self.reasoning,
            "render_time_ms": self.render_time_ms,
            "page_state": self.page_state,
        }
        return json.dumps(d, ensure_ascii=False)


# ─── Runner ───────────────────────────────────────────────────────────────────

# A ContextHook is an optional async callable invoked after the
# BrowserContext is created but before the first navigation. The smoke
# test uses it to install Playwright route interception so the runner
# can be exercised without a real dev server. Production runs pass None.
ContextHook = Callable[[Any], Awaitable[None]]


class PersonaRunner:
    """Run one persona through one app's routes.

    Parameters
    ----------
    persona
        Persona dict (already validated; see ``load_persona``).
    persona_id
        Stable id used in event tags + the output filename.
    app_config
        App config dict (already validated; see ``load_app_config``).
    runs_dir
        Where the JSONL session log lands. Defaults to the app_config's
        ``runs_dir`` resolved relative to ``app_config_dir``.
    app_config_dir
        Directory the app_config was loaded from; used to resolve runs_dir.
    headless
        Playwright headless mode. Default True.
    dev_server_override
        Optional URL to use instead of ``app_config["app"]["dev_server"]``.
        Useful for tests that point at a fixture server.
    context_hook
        Optional async callable receiving the freshly-created
        BrowserContext. Tests use this to install route interception.
    """

    def __init__(
        self,
        persona: dict[str, Any],
        persona_id: str,
        app_config: dict[str, Any],
        runs_dir: str | Path | None = None,
        app_config_dir: str | Path | None = None,
        headless: bool = True,
        dev_server_override: str | None = None,
        context_hook: ContextHook | None = None,
    ) -> None:
        self.persona = persona
        self.persona_id = persona_id
        self.app_config = app_config
        self.headless = headless
        self.dev_server = dev_server_override or app_config["app"]["dev_server"]
        self.context_hook = context_hook

        # Resolve runs_dir. If caller passed one, honour it. Otherwise read
        # from app_config["runs_dir"] and resolve against app_config_dir
        # (which the loader doesn't track — caller must supply).
        if runs_dir is not None:
            self.runs_dir = Path(runs_dir)
        elif app_config_dir is not None:
            self.runs_dir = Path(app_config_dir) / app_config["runs_dir"]
        else:
            self.runs_dir = Path(app_config["runs_dir"])

        self.events: list[SessionEvent] = []
        self.console_errors: list[str] = []
        self.session_path: Path | None = None

    # ─── Event helpers ────────────────────────────────────────────────────────

    def _record(self, **kwargs: Any) -> SessionEvent:
        ev = SessionEvent(persona_id=self.persona_id, **kwargs)
        self.events.append(ev)
        return ev

    # ─── Pure logic exposed for the analyzer / orchestrator ───────────────────

    @property
    def session_summary(self) -> dict[str, Any]:
        action_count = sum(1 for e in self.events if e.event_type == "action")
        nav_count = sum(1 for e in self.events if e.event_type == "nav")
        return {
            "persona_id": self.persona_id,
            "engagement": archetype_engagement(self.persona),
            "routes_visited": nav_count,
            "actions_taken": action_count,
            "errors": len(self.console_errors),
            "events": len(self.events),
            "session_path": str(self.session_path) if self.session_path else None,
        }

    # ─── Main entrypoint ──────────────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        """Execute the full session. Returns ``session_summary``."""
        # Import lazily so non-runner consumers (analyzer, behavior tests)
        # don't pay the playwright import cost.
        from playwright.async_api import async_playwright

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.runs_dir / f"{self.persona_id}-{_timestamp_slug()}.jsonl"

        self._record(
            event_type="reasoning",
            reasoning=(
                f"{self.persona['identity']['name']}: {self.persona['meta_attitude']} "
                f"Engagement: {archetype_engagement(self.persona)}."
            ),
        )

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            try:
                context = await browser.new_context(
                    user_agent=(
                        f"PersonaLab/0.1 ({self.persona_id}; "
                        f"+https://example.local/personalab)"
                    ),
                    viewport={"width": 1440, "height": 900},
                )
                if self.context_hook:
                    await self.context_hook(context)

                page = await context.new_page()
                page.on(
                    "console",
                    lambda msg: self._on_console(msg),
                )
                page.on(
                    "pageerror",
                    lambda err: self.console_errors.append(str(err)),
                )

                for route in self.app_config["routes"]:
                    await self._visit_route(page, route)

                await context.close()
            finally:
                await browser.close()

        self._persist()
        return self.session_summary

    # ─── Per-route work ───────────────────────────────────────────────────────

    async def _visit_route(self, page: Any, route: dict[str, Any]) -> None:
        url = self.dev_server.rstrip("/") + route["path"]
        before = time.monotonic()
        nav_error: str | None = None
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            status = response.status if response else None
        except Exception as exc:  # noqa: BLE001 — top-level isolation per route
            status = None
            nav_error = repr(exc)

        render_ms = int((time.monotonic() - before) * 1000)

        # Capture page state. If navigation failed we still emit a capture
        # event with the failure so the analyzer sees a friction signal.
        page_state: dict[str, Any] = {
            "url": url,
            "status": status,
            "title": "",
            "body_text_length": 0,
            "visible_action_names": [],
            "selector_probe": [],
            "console_errors": list(self.console_errors),
            "nav_error": nav_error,
        }
        if nav_error is None:
            try:
                page_state["title"] = await page.title()
                body_text = await page.evaluate("() => document.body.innerText || ''")
                page_state["body_text_length"] = len(body_text)
                probe = await _probe_route_actions(
                    page, route["actions"], self.app_config["actions"]
                )
                page_state["selector_probe"] = probe
                page_state["visible_action_names"] = _visible_names_from_probe(probe)
            except Exception as exc:  # noqa: BLE001
                page_state["capture_error"] = repr(exc)

        # Slow_load friction: did we exceed the route's budget?
        budget = route.get("expected_load_time_ms", 2000)
        slow = render_ms > budget

        self._record(
            event_type="nav",
            route=route["path"],
            render_time_ms=render_ms,
            page_state=page_state,
            reasoning=(
                f"Loaded in {render_ms}ms "
                f"(budget {budget}ms{'; SLOW' if slow else ''})."
            ),
        )

        if nav_error or status not in (None, 200, 304):
            # Don't try to interact with a broken page.
            return

        await self._take_route_actions(page, route, page_state["visible_action_names"])

        # Walk into detail routes the parent declared (e.g. /companies/[slug]
        # under /companies) when a prior action navigated the persona there.
        # Detail traversal is one level deep — nested detail-of-detail is out
        # of scope for the first runner pass.
        await self._maybe_walk_detail_routes(page, route)

    async def _take_route_actions(
        self,
        page: Any,
        route: dict[str, Any],
        visible_action_names: list[str],
    ) -> None:
        wanted = actions_for_route(self.persona, route)
        # Only attempt actions the runner could actually see.
        actually_take = [a for a in wanted if a in visible_action_names]

        # Note the gap between wanted and seen — that itself is friction
        # signal: persona wants the action, app doesn't surface it.
        missing = [a for a in wanted if a not in visible_action_names]
        if missing:
            self._record(
                event_type="reasoning",
                route=route["path"],
                reasoning=(
                    f"Persona would have taken {missing!r} but no matching "
                    f"affordance found on {route['path']}."
                ),
            )

        for action_name in actually_take:
            action_spec = _action_by_name(self.app_config["actions"], action_name)

            # Protected actions: log intent, never click. PersonaLab must
            # not mutate applications.md or other user-owned files even if
            # the page exposes the button. See behavior.is_protected_action.
            if is_protected_action(action_spec):
                self._record(
                    event_type="reasoning",
                    route=route["path"],
                    action=action_name,
                    selector=action_spec.get("selector"),
                    reasoning=(
                        f"Persona would have taken {action_name!r} but the action's "
                        f"side_effects {action_spec.get('side_effects')!r} include a "
                        "protected write; intent logged, click suppressed."
                    ),
                )
                continue

            await self._take_action(page, route, action_spec)
            await page.wait_for_timeout(click_delay_ms(self.persona))

            # Detail-reading dwell — if this action looks like an expansion,
            # the persona pauses to read.
            if self.persona["behavioral"]["reads_details"] and (
                "expand" in action_name or action_name.startswith("open_")
            ):
                dwell = detail_dwell_ms(self.persona)
                if dwell:
                    # In tests we don't want to actually sleep 80 seconds;
                    # the runner records the intended dwell but waits a
                    # capped amount.
                    self._record(
                        event_type="reasoning",
                        route=route["path"],
                        reasoning=f"Persona dwells {dwell}ms reading detail.",
                    )
                    await page.wait_for_timeout(min(dwell, 1500))

    # ─── Detail-route traversal ───────────────────────────────────────────────

    async def _maybe_walk_detail_routes(
        self,
        page: Any,
        parent_route: dict[str, Any],
    ) -> None:
        """If the parent route declares ``detail_routes`` and a prior action
        landed us on a matching URL, exercise the detail route's actions
        in place. Records the entry with an ``event_type='nav'`` event so
        the analyzer sees /companies/[slug] as a visited surface.
        """
        detail_routes = parent_route.get("detail_routes") or []
        if not detail_routes:
            return

        # The parent route's action that navigates here (e.g. open_company_detail)
        # only awaits the click — Playwright's default ``click`` does not wait
        # for the resulting navigation to commit. By the time we check
        # ``page.url`` immediately after, the in-flight nav may not yet have
        # settled and we'd compare against the parent URL. Found via C6
        # validation: ``open_company_detail`` fired against the real dashboard
        # but the detail traversal never triggered because the URL hadn't
        # changed yet at the read point.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:  # noqa: BLE001
            # If the wait times out the click may not have triggered a nav at
            # all — keep going so we still record whatever state we end up in.
            pass

        current_url = page.url
        # Strip dev_server prefix; compare path against detail_route patterns.
        from urllib.parse import urlparse
        current_path = urlparse(current_url).path or "/"

        for detail in detail_routes:
            if not _matches_path_pattern(current_path, detail["path"]):
                continue

            # Capture page state for the detail route — the persona is
            # already here, so render_time is 0 (the parent action's click
            # is what brought them here, and its dwell already happened).
            probe: list[dict[str, Any]] = []
            try:
                title = await page.title()
                body_text = await page.evaluate("() => document.body.innerText || ''")
                probe = await _probe_route_actions(
                    page, detail.get("actions", []), self.app_config["actions"]
                )
                visible = _visible_names_from_probe(probe)
            except Exception as exc:  # noqa: BLE001
                title, body_text, visible = "", "", []
                self._record(
                    event_type="error",
                    route=detail["path"],
                    reasoning=f"Detail capture failed: {exc!r}",
                )

            self._record(
                event_type="nav",
                route=detail["path"],
                render_time_ms=0,  # entered via prior action
                page_state={
                    "url": current_url,
                    "status": 200,  # we got here, so the parent action succeeded
                    "title": title,
                    "body_text_length": len(body_text),
                    "visible_action_names": visible,
                    "selector_probe": probe,
                    "console_errors": list(self.console_errors),
                    "entered_via": "detail_route_traversal",
                },
                reasoning=(
                    f"Entered detail route {detail['path']} after prior "
                    f"action navigated to {current_path}."
                ),
            )

            await self._take_route_actions(page, detail, visible)
            # First match wins — detail_routes is intended to be an
            # ordered list of distinct destinations, not multi-match.
            return

    async def _take_action(
        self,
        page: Any,
        route: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        selector = action["selector"]
        # An action whose side_effects include ``navigates_to:`` is expected
        # to commit a navigation. Without explicit nav-await, the post-click
        # ``page.url`` read race-conditions for fast personas (the click
        # returns before the SPA router has updated history). expect_navigation
        # explicitly waits for the new commit before returning, so subsequent
        # _maybe_walk_detail_routes() sees the destination URL.
        expects_nav = any(
            (se or "").startswith("navigates_to:")
            for se in (action.get("side_effects") or [])
        )
        try:
            locator = page.locator(selector).first
            if expects_nav:
                # If the click doesn't actually trigger a nav (e.g. the link
                # was already on the destination), expect_navigation raises;
                # swallow that and fall back to a plain click.
                try:
                    async with page.expect_navigation(
                        wait_until="domcontentloaded", timeout=3000,
                    ):
                        await locator.click(timeout=2000)
                except Exception:  # noqa: BLE001
                    await locator.click(timeout=2000)
            else:
                await locator.click(timeout=2000)
            self._record(
                event_type="action",
                route=route["path"],
                action=action["name"],
                selector=selector,
                reasoning=(
                    f"{action['name']} chosen for "
                    f"{self.persona['behavioral']['click_speed']}-speed "
                    f"{self.persona['behavioral']['rejection_threshold']}-threshold "
                    "persona."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self._record(
                event_type="error",
                route=route["path"],
                action=action["name"],
                selector=selector,
                reasoning=f"Action failed: {exc!r}",
            )

    # ─── Console plumbing ─────────────────────────────────────────────────────

    def _on_console(self, msg: Any) -> None:
        # Capture errors and warnings only — info/debug noise drowns signal.
        try:
            if msg.type in ("error", "warning"):
                self.console_errors.append(f"[{msg.type}] {msg.text}")
        except Exception:  # noqa: BLE001
            pass

    # ─── Persist ──────────────────────────────────────────────────────────────

    def _persist(self) -> None:
        assert self.session_path is not None
        with self.session_path.open("w", encoding="utf-8") as fh:
            for ev in self.events:
                fh.write(ev.to_jsonl())
                fh.write("\n")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _action_by_name(actions: Iterable[dict[str, Any]], name: str) -> dict[str, Any]:
    for a in actions:
        if a["name"] == name:
            return a
    raise KeyError(f"Action {name!r} not defined in app_config")


def _matches_path_pattern(actual_path: str, pattern: str) -> bool:
    """Match an actual URL path against an app-config path pattern.

    Patterns use ``[name]`` for dynamic segments (matching Next.js
    convention). ``/companies/[slug]`` matches ``/companies/anthropic``
    but not ``/companies`` or ``/companies/anthropic/roles``.

    Static patterns require exact match. The pattern must start with ``/``.
    """
    actual_parts = [p for p in actual_path.split("/") if p]
    pattern_parts = [p for p in pattern.split("/") if p]
    if len(actual_parts) != len(pattern_parts):
        return False
    for a, p in zip(actual_parts, pattern_parts):
        if p.startswith("[") and p.endswith("]"):
            # Dynamic segment — accept any non-empty value.
            if not a:
                return False
        elif a != p:
            return False
    return True


async def _probe_route_actions(
    page: Any,
    route_action_names: Iterable[str],
    all_actions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Probe each declared action's selector and return structured results.

    Each entry is ``{name, selector, matched_count, eval_error}``:

    - ``matched_count``: how many DOM elements the selector resolved to.
      0 = no match. >=1 = present.
    - ``eval_error``: ``repr(exc)`` if Playwright raised while evaluating
      the selector (invalid syntax, page navigated away, etc.), else None.

    The previous ``_visible_action_names`` collapsed three distinct
    conditions into one ``[]`` output (no match / eval error / unknown
    action), which is the root of the C6 false-positive cascade — the
    analyzer couldn't distinguish "no affordance" from "stale selector"
    from "page not hydrated." The structured form lets it.
    """
    by_name = {a["name"]: a for a in all_actions}
    out: list[dict[str, Any]] = []
    for name in route_action_names:
        spec = by_name.get(name)
        if not spec:
            out.append({
                "name": name,
                "selector": None,
                "matched_count": 0,
                "eval_error": "action_not_defined_in_app_config",
            })
            continue
        selector = spec["selector"]
        try:
            count = await page.locator(selector).count()
            out.append({
                "name": name,
                "selector": selector,
                "matched_count": int(count),
                "eval_error": None,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({
                "name": name,
                "selector": selector,
                "matched_count": 0,
                "eval_error": repr(exc),
            })
    return out


def _visible_names_from_probe(probe: list[dict[str, Any]]) -> list[str]:
    """Back-compat extraction: names whose selector matched at least one
    element. Kept as a separate function (vs inline) so the analyzer's
    trimmed event payload can still expose ``visible_action_names``
    while the structured probe carries the richer signal."""
    return [p["name"] for p in probe if p.get("matched_count", 0) > 0]


# ─── Convenience: read a session JSONL back ───────────────────────────────────

def read_session(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSONL session log into a list of dicts. Useful for the
    analyzer and replayer."""
    events: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


__all__ = [
    "PersonaRunner",
    "SessionEvent",
    "read_session",
]
