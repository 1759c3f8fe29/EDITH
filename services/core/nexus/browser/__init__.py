"""Driving a real browser, rather than handing a URL to one (task C8).

Everything in ``nexus.tools.browser`` stops at the edge of the process: it can ask
the desktop to open a page and it can download one, but it cannot see what a
browser *rendered* or touch a control on it. That gap is the difference between
"open my bank's site" and "check whether the payment went through".

This package closes it by speaking the Chrome DevTools Protocol to a browser NEXUS
starts itself. See :mod:`nexus.browser.driver` for how that browser is kept
separate from the one the user is actually using.
"""

from __future__ import annotations

from .driver import BrowserUnavailable, ChromeSession, session  # noqa: F401

__all__ = ["BrowserUnavailable", "ChromeSession", "session"]
