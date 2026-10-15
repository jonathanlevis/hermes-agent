"""P2 (docs/hermes-cua-improvements.md): the active-window watchdog invalidates the cua-driver
backend's sticky target when the WM's `_NET_ACTIVE_WINDOW` moves off it (F3 — Playwright's
juggler-pipe recreates Camoufox windows mid-session and the next input would hit
`X11Error code 3`), and the next input action silently re-binds via `focus_app(last_app)`.

Signal semantics: a WM-less display (probe None / window 0) is "no signal" — never an
invalidation. Everything here is host-independent (probe and focus are injected fakes).
"""

import threading
from types import SimpleNamespace

from tools.computer_use.backend import ActionResult
from tools.computer_use.cua_backend_watchdog import _ActiveWindowWatchdog, _is_linux_x11_session


class _StubBackend:
    """The watchdog's exact view of a backend: sticky-target fields + the invalidation hook."""

    def __init__(self, window_id=0x11, app="camoufox-default"):
        self._active_pid = 4242
        self._active_window_id = window_id
        self._last_app = app
        self._last_target = {"pid": 4242, "window_id": window_id}
        self._snapshot_tokens = {1: "tok"}
        self._rebind_pending = False
        self.invalidated = threading.Event()

    def _invalidate_sticky_target(self):
        self._active_pid = self._active_window_id = self._last_target = None
        self._snapshot_tokens = {}
        self._rebind_pending = True
        self.invalidated.set()


def test_active_window_change_invalidates_but_keeps_last_app():
    backend = _StubBackend(window_id=0x11)
    wd = _ActiveWindowWatchdog(backend, probe=lambda: 0x22)
    assert wd.check_once() is True
    assert backend.invalidated.is_set()
    assert backend._active_window_id is None and backend._active_pid is None
    assert backend._last_app == "camoufox-default"  # survives for the silent re-bind
    assert backend._rebind_pending is True
    assert backend._snapshot_tokens == {}  # stale tokens can't pair with the new target


def test_matching_active_window_is_a_no_op():
    backend = _StubBackend(window_id=0x11)
    wd = _ActiveWindowWatchdog(backend, probe=lambda: 0x11)
    assert wd.check_once() is False
    assert not backend.invalidated.is_set()


def test_wm_less_display_is_no_signal_never_invalidates():
    for no_signal in (None, 0):
        backend = _StubBackend(window_id=0x11)
        wd = _ActiveWindowWatchdog(backend, probe=lambda: no_signal)
        assert wd.check_once() is False
        assert not backend.invalidated.is_set()


def test_probe_exception_is_swallowed():
    def boom():
        raise RuntimeError("xprop missing")

    backend = _StubBackend()
    wd = _ActiveWindowWatchdog(backend, probe=boom)
    assert wd.check_once() is False
    assert not backend.invalidated.is_set()


def test_no_sticky_target_means_nothing_to_do():
    backend = _StubBackend()
    backend._active_window_id = None
    wd = _ActiveWindowWatchdog(backend, probe=lambda: 0x22)
    assert wd.check_once() is False


def test_start_is_a_noop_without_x11(monkeypatch):
    monkeypatch.setenv("DISPLAY", "")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not _is_linux_x11_session({"DISPLAY": ""})
    backend = _StubBackend()
    wd = _ActiveWindowWatchdog(backend, probe=lambda: 0x22)
    wd.start()
    try:
        assert wd._thread is None  # never spawned off an X11 session
    finally:
        wd.stop()


# ── silent re-bind through the input mixin ────────────────────────────────────
from tools.computer_use.cua_backend_input import _InputMixin


class _RebindFake(_InputMixin):
    """Input-mixin consumer with an injectable focus_app."""

    def __init__(self, focus_result):
        self._active_pid = None
        self._active_window_id = None
        self._last_app = "camoufox-default"
        self._rebind_pending = True
        self._focus_calls = []
        self._focus_result = focus_result

    def focus_app(self, app, raise_window=False):
        self._focus_calls.append((app, raise_window))
        if isinstance(self._focus_result, Exception):
            raise self._focus_result
        if self._focus_result:
            self._active_pid, self._active_window_id = 99, 0x33
        return self._focus_result


def test_target_args_rebinds_silently_once():
    fake = _RebindFake(ActionResult(ok=True, action="focus_app"))
    refusal, args = fake._target_args("click")
    assert refusal is None and args == {"pid": 99}
    assert fake._focus_calls == [("camoufox-default", False)]  # no raise_window
    assert fake._rebind_pending is False


def test_target_args_needs_window_rebind_resolves_window_id():
    fake = _RebindFake(ActionResult(ok=True, action="focus_app"))
    refusal, args = fake._target_args("type_text", need_window=True)
    assert refusal is None and args == {"pid": 99, "window_id": 0x33}


def test_failed_rebind_falls_back_to_refusal_without_looping():
    fake = _RebindFake(ActionResult(ok=False, action="focus_app"))
    refusal, _ = fake._target_args("click")
    assert refusal is not None and "No active window" in refusal.message
    assert len(fake._focus_calls) == 1  # disarmed after one attempt


def test_rebind_exception_degrades_to_refusal():
    fake = _RebindFake(RuntimeError("driver down"))
    refusal, _ = fake._target_args("click")
    assert refusal is not None


def test_no_pending_rebind_means_no_focus_call():
    fake = _RebindFake(ActionResult(ok=True, action="focus_app"))
    fake._rebind_pending = False
    refusal, _ = fake._target_args("click")
    assert refusal is not None and fake._focus_calls == []


# ── action-time dead-window retry (the watchdog can lose the poll race) ──────
class _DeadWindowFake(_RebindFake):
    """_action fails once with the live F3 BadWindow signature, then succeeds after a re-bind."""

    def __init__(self, dead_window_id):
        super().__init__(focus_result=ActionResult(ok=True, action="focus_app"))
        self._active_pid = 55
        self._active_window_id = dead_window_id
        self._rebind_pending = False
        self._session = SimpleNamespace()  # _run_input_action probes _has_tool only for bring_to_front
        self._action_calls = []

    def _action(self, name, args, inject_session=True):
        self._action_calls.append((name, dict(args)))
        if len(self._action_calls) == 1:
            msg = (f"cua-driver {name} failed: X11 error X11Error {{ error_kind: Window, error_code: 3, "
                   f"sequence: 1, bad_value: {args.get('window_id') or args.get('pid')}, minor_opcode: 0, "
                   "major_opcode: 40 }}")
            return ActionResult(ok=False, action=name, message=msg, meta={"detail": msg})
        return ActionResult(ok=True, action=name)

    def _invalidate_sticky_target(self):
        self._rebind_pending = True  # armed; focus_app (from _RebindFake) then sets pid 99 / 0x33


def test_dead_window_error_rebinds_and_reissues_once():
    from types import SimpleNamespace as _NS  # used by the fake below
    fake = _DeadWindowFake(dead_window_id=0x2B)
    result = fake._run_input_action("click", {"pid": 55, "window_id": 0x2B}, None, False)
    assert result.ok is True
    assert len(fake._action_calls) == 2
    # The re-issued action carries the RE-BOUND target, not the dead one.
    assert fake._action_calls[1][1] == {"pid": 99, "window_id": 0x33}
    assert fake._focus_calls == [("camoufox-default", False)]


def test_foreign_bad_window_error_is_not_retried():
    class _NoRetry(_DeadWindowFake):
        def _action(self, name, args, inject_session=True):
            self._action_calls.append((name, dict(args)))
            msg = ("X11 error X11Error { error_kind: Window, error_code: 3, sequence: 1, "
                   "bad_value: 12345, minor_opcode: 0 }")
            return ActionResult(ok=False, action=name, message=msg)

    fake = _NoRetry(dead_window_id=0x2B)
    result = fake._run_input_action("click", {"pid": 55, "window_id": 0x2B}, None, False)
    assert result.ok is False and len(fake._action_calls) == 1 and fake._focus_calls == []


def test_non_x11_failure_is_not_retried():
    class _PlainFail(_DeadWindowFake):
        def _action(self, name, args, inject_session=True):
            self._action_calls.append((name, dict(args)))
            return ActionResult(ok=False, action=name, message="connection refused")

    fake = _PlainFail(dead_window_id=0x2B)
    result = fake._run_input_action("click", {"pid": 55, "window_id": 0x2B}, None, False)
    assert result.ok is False and len(fake._action_calls) == 1 and fake._focus_calls == []


def test_dead_window_with_nothing_to_rebind_returns_actionable_error():
    class _NothingLeft(_DeadWindowFake):
        def __init__(self, dead_window_id):
            super().__init__(dead_window_id)
            self._focus_result = ActionResult(ok=False, action="focus_app",
                                              message="No on-screen window found for app 'camoufox-default'.")

    fake = _NothingLeft(dead_window_id=0x2B)
    result = fake._run_input_action("click", {"pid": 55, "window_id": 0x2B}, None, False)
    assert result.ok is False and result.code == "target_window_destroyed"
    assert "X11Error" not in result.message and "fresh capture" in result.message
    assert len(fake._action_calls) == 1
