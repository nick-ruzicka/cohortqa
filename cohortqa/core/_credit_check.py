"""Credit-exhausted detection for Anthropic API errors.

Mirrors the JS-side handling in scripts/generate-briefing.mjs and the
dashboard's /api/briefing and /api/chat routes: when the Anthropic API
returns a 400 with a credit-balance message, we want callers to see a
clean ``CreditsExhaustedError`` with a top-up URL — not the raw SDK
``BadRequestError`` traceback.

The detection is text-pattern based against the exception's string form,
which works for both the typed ``anthropic.BadRequestError`` and any
generic exception that includes the API body in its repr.
"""

from __future__ import annotations

import re

TOP_UP_URL = "https://console.anthropic.com/settings/billing"

_CREDIT_PATTERN = re.compile(
    r"credit balance|credits.*too low|purchase credits",
    re.IGNORECASE,
)


class CreditsExhaustedError(RuntimeError):
    """Anthropic API credits are exhausted. Top up to continue."""

    def __init__(self, api_message: str = "", *, top_up_url: str = TOP_UP_URL) -> None:
        self.api_message = api_message
        self.top_up_url = top_up_url
        super().__init__(
            f"Anthropic API credits are exhausted. Top up to continue: {top_up_url} "
            f"(API said: {api_message[:200]})"
        )


def reraise_if_credits_exhausted(exc: BaseException) -> None:
    """If ``exc`` looks like an Anthropic credit-balance error, raise a
    ``CreditsExhaustedError`` instead. Otherwise, return silently so the
    caller can re-raise the original exception.

    Usage::

        try:
            response = self.client.messages.parse(**kwargs)
        except Exception as exc:
            reraise_if_credits_exhausted(exc)
            raise  # not a credit error — propagate original
    """
    text = str(exc)
    if _CREDIT_PATTERN.search(text):
        raise CreditsExhaustedError(text) from exc
