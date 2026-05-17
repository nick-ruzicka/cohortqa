"""End-to-end smoke test for PersonaRunner.

Drives the runner against a Playwright-intercepted route that serves a
small synthetic HTML page per route — no dev server, no network, no
external dependencies beyond chromium being installed.

Skipped automatically if Playwright + chromium aren't available so the
schema tests still pass in environments without browsers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip the whole module if playwright isn't importable (e.g. CI without it).
playwright = pytest.importorskip("playwright.async_api")


# ─── Synthetic pages ──────────────────────────────────────────────────────────

SYNTHETIC_PAGES = {
    "/today": """
        <!doctype html><html><head><title>Today</title></head>
        <body>
          <h1>Today's roles</h1>
          <button>Run Scan</button>
          <table>
            <tr role="button" class="cursor-pointer"><td>Role A</td></tr>
          </table>
        </body></html>
    """,
    "/pipeline": """
        <!doctype html><html><head><title>Pipeline</title></head>
        <body>
          <h1>Pipeline</h1>
          <button>Run Scan</button>
          <button>Signal Scan</button>
          <table>
            <tr role="button" class="cursor-pointer">
              <td>Role X</td>
              <td><a href="/companies/acme">Acme</a></td>
              <td><a href="https://jobs.example.com/x" target="_blank">Apply</a></td>
            </tr>
          </table>
          <button>Skipped</button>
          <button>Evaluated</button>
        </body></html>
    """,
    # /companies, /signals, /context, /companies/[slug] return minimal
    # pages that load but offer no matching actions — the runner should
    # log "no affordance found" reasoning events.
    "_DEFAULT_": """
        <!doctype html><html><head><title>Page</title></head>
        <body><h1>Other page</h1></body></html>
    """,
}


def _make_app_config(tmp_path: Path) -> dict:
    """A minimal app config exercising the runner's main paths."""
    return {
        "app": {
            "name": "SmokeApp",
            "dev_server": "http://test.local",
            "description": "Synthetic app for runner smoke test.",
        },
        "routes": [
            {
                "path": "/today",
                "purpose": "Smoke today.",
                "actions": ["run_scan", "click_role_row"],
                "expected_load_time_ms": 5000,
            },
            {
                "path": "/pipeline",
                "purpose": "Smoke pipeline.",
                "actions": [
                    "run_scan",
                    "click_role_row",
                    "mark_skipped",
                    "open_company_from_row",
                ],
                "expected_load_time_ms": 5000,
            },
        ],
        "actions": [
            {"name": "run_scan", "selector": 'button:has-text("Run Scan")'},
            {
                "name": "click_role_row",
                "selector": 'tr[role="button"]',
                "requires": ["role_visible"],
            },
            {"name": "mark_skipped", "selector": 'button:has-text("Skipped")'},
            {
                "name": "open_company_from_row",
                "selector": 'a[href^="/companies/"]',
            },
        ],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
            {"type": "slow_load", "description": "Slow."},
        ],
        "personas_dir": "personas",
        "scenarios_dir": "scenarios",
        "runs_dir": str(tmp_path / "runs"),
    }


def _persona() -> dict:
    return {
        "identity": {
            "name": "Smoke Persona",
            "role": "Test",
            "background": "Smokes tests for a living.",
        },
        "target_archetypes": ["smoke"],
        "location_preferences": [],
        "comp_floor": 100000,
        "behavioral": {
            "click_speed": "fast",          # keep test fast: 300ms delays
            "reads_details": True,
            "rejection_threshold": "low",   # so mark_skipped is chosen
            "detail_dwell_ms": 100,
        },
        "meta_attitude": "Smoke-testing.",
        "friction_sensitivities": ["navigation"],
    }


# ─── The smoke tests ──────────────────────────────────────────────────────────
# Driven via asyncio.run from sync test functions so pytest-asyncio isn't
# a required dependency.


def test_runner_smoke_end_to_end(tmp_path):
    """Run a session through two synthetic routes; verify the JSONL log
    has the events we'd expect to see."""
    import asyncio

    from personalab.core.runner import PersonaRunner, read_session

    async def install_intercept(context):
        async def handler(route, request):
            # Strip the dev_server prefix and look up by path.
            path = "/" + request.url.split("/", 3)[-1] if "://" in request.url else request.url
            # request.url is "http://test.local/today" → path becomes "/today"
            # Re-parse robustly:
            import urllib.parse
            parsed = urllib.parse.urlparse(request.url)
            page_path = parsed.path or "/"
            body = SYNTHETIC_PAGES.get(page_path, SYNTHETIC_PAGES["_DEFAULT_"])
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=body,
            )

        await context.route("**/*", handler)

    runner = PersonaRunner(
        persona=_persona(),
        persona_id="smoke",
        app_config=_make_app_config(tmp_path),
        headless=True,
        context_hook=install_intercept,
    )

    summary = asyncio.run(runner.run())

    # ─── Assertions on the summary ────────────────────────────────────────────
    assert summary["persona_id"] == "smoke"
    assert summary["routes_visited"] == 2
    assert summary["actions_taken"] >= 2  # at least run_scan + one row action
    assert summary["session_path"], summary

    # ─── Assertions on the JSONL on disk ──────────────────────────────────────
    session_path = Path(summary["session_path"])
    assert session_path.exists()
    events = read_session(session_path)
    assert len(events) >= 5  # opening reasoning + 2 navs + actions

    # Every event tagged with personalab source for analytics filtering.
    for ev in events:
        assert ev["source"] == "personalab:smoke", ev

    # We hit both routes.
    nav_routes = [ev["route"] for ev in events if ev["event_type"] == "nav"]
    assert nav_routes == ["/today", "/pipeline"]

    # At least one action recorded with the run_scan name.
    actions = [ev for ev in events if ev["event_type"] == "action"]
    assert any(ev["action"] == "run_scan" for ev in actions), actions

    # Every nav event has render_time_ms and page_state.
    for ev in events:
        if ev["event_type"] == "nav":
            assert isinstance(ev["render_time_ms"], int)
            assert ev["render_time_ms"] >= 0
            assert ev["page_state"] is not None
            assert ev["page_state"]["url"].endswith(ev["route"])


def test_runner_logs_missing_affordances(tmp_path):
    """If a persona would take an action that the page doesn't expose,
    the runner records a reasoning event flagging the gap."""
    import asyncio

    from personalab.core.runner import PersonaRunner, read_session

    async def empty_intercept(context):
        async def handler(route, request):
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="<!doctype html><html><body><h1>Empty</h1></body></html>",
            )
        await context.route("**/*", handler)

    cfg = _make_app_config(tmp_path)
    # Trim to one route to keep the assertion focused.
    cfg["routes"] = cfg["routes"][:1]

    runner = PersonaRunner(
        persona=_persona(),
        persona_id="smoke",
        app_config=cfg,
        headless=True,
        context_hook=empty_intercept,
    )
    asyncio.run(runner.run())

    events = read_session(runner.session_path)
    missing_reasons = [
        ev for ev in events
        if ev["event_type"] == "reasoning"
        and ev.get("reasoning") and "no matching affordance" in ev["reasoning"]
    ]
    assert missing_reasons, [
        (e["event_type"], e.get("reasoning")) for e in events
    ]
