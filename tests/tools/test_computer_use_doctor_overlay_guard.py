"""P4 (docs/hermes-cua-improvements.md): `hermes computer-use doctor` recovers from the F1 wedged
cursor-overlay window (Cua.AgentCursorOverlay.* surviving an unclean cua-driver session end).

The guard xkills an overlay ONLY when its `_NET_WM_PID` owner is dead — xkill severs the owner's
whole X connection, so a live owner (a running daemon's own overlay) must never be killed.
"""

from tools.computer_use import doctor


def _report(overall="ok"):
    return {"overall": overall, "checks": [{"name": "binary_version", "status": "pass"}]}


def _guard(monkeypatch, *, tree, pids, killed=True, x11=True):
    monkeypatch.setattr(doctor, "_sanitized_cua_env",
                        lambda: {"DISPLAY": ":20"} if x11 else {})
    monkeypatch.setattr(doctor, "_probe_root_tree", lambda env: tree)
    monkeypatch.setattr(doctor, "_probe_window_pid", lambda wid, env: pids.get(wid))
    monkeypatch.setattr(doctor, "_xkill_window", lambda wid, env: killed)
    return doctor._apply_wedged_overlay_guard(_report(), binary="cua-driver")


_TREE = """
xwininfo: Window id: 0x22c (the root window) (has no name)

  Root window id: 0x22c (the root window) (has no name)
     22 children:
     0xc00001 "Firefox": ("camoufox-default" "Camoufox-default")  1380x800+60+114  +60+114
     0x4a00009 "Cua.AgentCursorOverlay.default": ("overlay" "overlay")  1440x900+0+0  +0+0
     0x4a0000e "Cua.AgentCursorOverlay.hermes-abc": ("overlay" "overlay")  1440x900+0+0  +0+0
"""


def test_parse_overlay_windows_finds_only_overlays():
    assert doctor._parse_overlay_windows(_TREE) == [0x4a00009, 0x4a0000e]


def test_dead_owner_overlay_is_cleared_and_reported(monkeypatch):
    dead_pid = 999999999  # no such process
    out = _guard(monkeypatch, tree=_TREE, pids={0x4a00009: dead_pid, 0x4a0000e: dead_pid})
    rows = [c for c in out["checks"] if c["name"] == "wedged cursor overlay"]
    assert len(rows) == 2 and all(r["status"] == "pass" and r["data"]["killed"] is True for r in rows)
    assert out["overall"] == "ok"  # recovery keeps the report green


def test_live_owner_overlay_is_killed_too_with_daemon_hint(monkeypatch):
    """The real F1 wedge keeps the overlay mapped by the STILL-RUNNING daemon — xkill is the remedy
    (the daemon's X connection drops with it), and the row says so."""
    live_pid = 1  # init never exits
    out = _guard(monkeypatch, tree=_TREE, pids={0x4a00009: live_pid, 0x4a0000e: live_pid})
    rows = [c for c in out["checks"] if c["name"] == "wedged cursor overlay"]
    assert len(rows) == 2 and all(r["status"] == "pass" for r in rows)
    assert all("restart the cua-driver daemon" in r["message"] for r in rows)


def test_mixed_owners_kill_both_and_report_each(monkeypatch):
    dead_pid = 999999999
    out = _guard(monkeypatch, tree=_TREE, pids={0x4a00009: dead_pid, 0x4a0000e: 1})
    rows = [c for c in out["checks"] if c["name"] == "wedged cursor overlay"]
    assert {r["data"]["window_id"] for r in rows} == {0x4a00009, 0x4a0000e}


def test_failed_xkill_degrades_the_overall(monkeypatch):
    dead_pid = 999999999
    out = _guard(monkeypatch, tree=_TREE, pids={0x4a00009: dead_pid}, killed=False)
    rows = [c for c in out["checks"] if c["name"] == "wedged cursor overlay"]
    assert rows[0]["status"] == "fail" and "xkill -id" in rows[0]["message"]
    assert out["overall"] == "degraded"


def test_no_x11_environment_is_silent(monkeypatch):
    called = []
    monkeypatch.setattr(doctor, "_probe_root_tree", lambda env: called.append(env) or _TREE)
    out = _guard(monkeypatch, tree=_TREE, pids={}, x11=False)
    assert not called and out["overall"] == "ok" and len(out["checks"]) == 1


def test_no_overlays_leaves_report_untouched(monkeypatch):
    out = _guard(monkeypatch, tree="xwininfo: Window id: 0x22c\n  0 children:\n", pids={})
    assert len(out["checks"]) == 1 and out["overall"] == "ok"
