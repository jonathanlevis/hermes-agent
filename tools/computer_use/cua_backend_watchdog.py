"""Sticky-target invalidation for the cua-driver backend (F3 / P2 in ``docs/hermes-cua-improvements.md``).

A background thread polls ``_NET_ACTIVE_WINDOW`` (the same xprop probe the capture side already
uses) and, when the window manager's active window stops matching the backend's cached
``_active_window_id``, invalidates the sticky target but KEEPS ``_last_app`` so the next input
action can silently re-bind via ``focus_app(app, raise_window=False)`` — no bring_to_front, no
crash with ``X11Error code 3 (Window)`` when Playwright's juggler-pipe recreates the target
window mid-session.

Signal semantics: a WM-less display (or a failed probe) reports no property / window id 0 —
that is "no signal", never an invalidation. Polling (not XSelectInput) deliberately: no new
dependency, bounded 1-interval latency, and the probe already exists.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Callable, Optional

from tools.computer_use.cua_backend_capture import _linux_x11_active_window_id

logger = logging.getLogger("tools.computer_use.cua_backend")

# The invalidation contract: these fields are cleared, `_last_app` survives for the re-bind and
# `_rebind_pending` arms the silent re-bind in `_InputMixin._target_args`.
_REBIND_ATTR = "_rebind_pending"


def _is_linux_x11_session(env: Optional[dict] = None) -> bool:
    """True on Linux with an X11 display — mirrors ``_cua_no_overlay``'s X11 branch (Wayland's
    compositor owns focus semantics and the xprop probe is X11-only anyway)."""
    check = dict(os.environ if env is None else env)
    return sys.platform == "linux" and bool(check.get("DISPLAY")) \
        and check.get("XDG_SESSION_TYPE") != "wayland" and not check.get("WAYLAND_DISPLAY")


class _ActiveWindowWatchdog:
    """Polls the WM's active window and invalidates the backend's sticky target on a change.

    Deliberately minimal concurrency: every write the watchdog performs goes through
    ``backend._invalidate_sticky_target()`` (plain attribute writes, CPython-atomic), and a race
    with an in-flight action only costs that action a stale-window error the next call already
    recovers from.
    """

    def __init__(self, backend: Any, probe: Optional[Callable[[], Optional[int]]] = None,
                 interval: float = 1.0) -> None:
        self._backend = backend
        self._probe = probe or _linux_x11_active_window_id
        self._interval = max(0.05, interval)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start polling; a no-op when not on a Linux/X11 session or already running."""
        if not _is_linux_x11_session():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        # spawn_context_thread binds the spawner's contextvars (profile scope) — a bare Thread
        # would drop them (HX012; one process serves many profiles).
        from agent.memory_provider import spawn_context_thread
        self._thread = spawn_context_thread(self._run, name="cua-active-window-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.check_once()

    def check_once(self) -> bool:
        """One poll. True when the sticky target was invalidated (exposed for tests and for
        callers that prefer to drive the check synchronously)."""
        backend = self._backend
        cached = getattr(backend, "_active_window_id", None)
        if cached is None:
            return False  # nothing sticky to invalidate
        try:
            active = self._probe()
        except Exception:  # a failed probe must never take the backend down; logged for diagnosis
            logger.debug("active-window probe failed", exc_info=True)
            return False
        if not active or active == cached:
            return False  # no WM signal (0x0/absent) or still focused — never invalidate
        app = getattr(backend, "_last_app", None)
        logger.debug("active window %s != sticky target %s (app %r): invalidating for silent re-bind",
                     hex(active), hex(cached), app)
        backend._invalidate_sticky_target()
        return True
