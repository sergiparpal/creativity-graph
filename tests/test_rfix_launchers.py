"""Regression tests for the two launcher supervisor-policy fixes (group: launchers).

These drive the SHIPPED ``scripts/launch_server.mjs`` exported pure functions by file URL under
``node --input-type=module`` — the same mechanism ``tests/test_launchers.py`` uses — so they bind to
the real ``restartDecision`` decision table, never a copy.

Two fixes:
  1. A non-forwarded CRASH signal (SIGSEGV/SIGABRT/… on import, before ``initialize`` is served) must NOT
     be mislabeled a clean exit; it has to fall through to the startup self-heal path. A GRACEFUL terminate
     signal (SIGTERM/SIGINT that is not our flagged shutdown) stays a clean exit.
  2. A crash WITHOUT the readiness marker (servedInit=false) is a startup failure; the marker is the
     authoritative gate. The marker-absent time window (EARLY_FAILURE_MS) is only a secondary tie-breaker
     and was widened so a slow-surfacing startup import failure still earns the one-time heal.

Node-dependent tests skip cleanly when ``node`` is not on PATH; nothing here spawns a real engine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_LAUNCH_MJS = REPO / "scripts" / "launch_server.mjs"
_LAUNCH_URL = json.dumps(_LAUNCH_MJS.as_uri())
NODE = shutil.which("node")


def _run_node_harness(body: str) -> dict:
    """Run a node ESM harness that imports the launcher and prints a JSON line; return the parsed dict."""
    script = f"import * as L from {_LAUNCH_URL};\n" + body
    r = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_startup_crash_signal_heals_not_clean_exit():
    """Fix 1: a CRASH signal (SIGSEGV, code null) during startup — !servedInit, small ranForMs — must
    fall through to the one-time heal, NOT be swallowed as a clean-exit as the old ``signal || !code``
    short-circuit did."""
    out = _run_node_harness(r"""
const out = {};
// SIGSEGV on import: signal set, code null (killed by signal), no readiness marker, died fast.
out.segv  = L.restartDecision({code:null, signal:"SIGSEGV", ranForMs:200,
                               shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:false});
// SIGABRT is likewise a crash -> heal.
out.abrt  = L.restartDecision({code:null, signal:"SIGABRT", ranForMs:200,
                               shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:false}).action;
// After the one heal is spent, a repeated crash-signal startup failure retries in place (bounded).
out.retry = L.restartDecision({code:null, signal:"SIGSEGV", ranForMs:200,
                               shuttingDown:false, triedHeal:true, recentRestartCount:0, servedInit:false});
process.stdout.write(JSON.stringify(out));
""")
    # The core regression: a startup crash signal heals rather than exiting clean.
    assert out["segv"]["action"] == "heal"
    assert out["segv"]["reason"] == "early-failure-heal"
    assert out["abrt"] == "heal"
    assert out["retry"]["action"] == "relaunch" and out["retry"]["reason"] == "startup-retry"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_graceful_terminate_signal_stays_clean_exit():
    """Fix 1 must NOT over-reach: a GRACEFUL terminate signal (SIGTERM/SIGINT) that is not a flagged
    shutdown still classifies as a clean exit (code 0) — only hard crash signals fall through."""
    out = _run_node_harness(r"""
const out = {};
out.term = L.restartDecision({code:1, signal:"SIGTERM", ranForMs:10,
                              shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:false});
out.int  = L.restartDecision({code:1, signal:"SIGINT", ranForMs:10,
                              shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:false});
process.stdout.write(JSON.stringify(out));
""")
    assert out["term"]["action"] == "exit" and out["term"]["reason"] == "clean-exit"
    assert out["term"]["code"] == 0
    assert out["int"]["action"] == "exit" and out["int"]["reason"] == "clean-exit"
    assert out["int"]["code"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_genuine_clean_exit_still_clean():
    """Fix 1 sanity: a genuine clean exit (code 0, no signal) that had served init is clean-exit."""
    out = _run_node_harness(r"""
const out = L.restartDecision({code:0, signal:null, ranForMs:1e9,
                               shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:true});
process.stdout.write(JSON.stringify(out));
""")
    assert out["action"] == "exit"
    assert out["reason"] == "clean-exit"
    assert out["code"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_slow_marker_absent_startup_crash_heals_within_widened_window():
    """Fix 2 (mitigation): a marker-absent startup crash that takes several seconds to surface — longer
    than the OLD 5s window but inside the WIDENED EARLY_FAILURE_MS — now earns the one-time heal instead
    of being misread as post-init. servedInit remains the authoritative gate.

    ranForMs=10000 was > the old 5000ms window (=> would have been post-init-exit) but is < the widened
    window, so it now heals. This is the concrete improvement toward 'marker absent => startup'."""
    out = _run_node_harness(r"""
const S = L.SUPERVISOR;
const out = { window: S.EARLY_FAILURE_MS };
// slow-surfacing startup import failure: no marker, non-zero code, ran ~10s.
out.slow = L.restartDecision({code:1, signal:null, ranForMs:10000,
                              shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:false});
process.stdout.write(JSON.stringify(out));
""")
    assert out["window"] >= 10000, "the marker-absent startup window is widened past the old 5s"
    assert out["slow"]["action"] == "heal", "a slow marker-absent startup crash now heals"
    assert out["slow"]["reason"] == "early-failure-heal"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_marker_present_fast_crash_is_post_init_regardless_of_time():
    """servedInit is the authoritative primary gate: a crash with the readiness marker present is post-init
    even when it died fast (well inside the window) — it must exit cleanly with the child's code, never
    heal onto the held-open, already-handshaked pipe. Guards that widening the window did not weaken the
    marker gate."""
    out = _run_node_harness(r"""
const out = L.restartDecision({code:70, signal:null, ranForMs:200,
                               shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:true});
process.stdout.write(JSON.stringify(out));
""")
    assert out["action"] == "exit"
    assert out["reason"] == "post-init-exit"
    assert out["code"] == 70


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_marker_absent_beyond_window_still_post_init_bounded():
    """A marker-absent crash that ran well past the widened window is still post-init (the time tie-breaker
    remains, keeping the classification bounded); the MAX_RESTARTS crash-loop cap otherwise bounds any
    marker-write-failure thrashing inside the window."""
    out = _run_node_harness(r"""
const out = L.restartDecision({code:70, signal:null, ranForMs:40000,
                               shuttingDown:false, triedHeal:true, recentRestartCount:1, servedInit:false});
process.stdout.write(JSON.stringify(out));
""")
    assert out["action"] == "exit"
    assert out["reason"] == "post-init-exit"
    assert out["code"] == 70
