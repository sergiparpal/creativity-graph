"""Stage 3: the convergence harness gate (§4).

Mirrors the specificity gate (§1.6): convergence stays ADVISORY until a HISTORY of past candidates
shows the high-convergence band grounds materially more than the low band. Until then convergence is
displayed but decides nothing (G5).
"""
from __future__ import annotations

import json

from kg_engine.harness import _main, convergence


def test_convergence_gate_on_when_high_band_grounds_more():
    # high band (convergence>=2) grounds 4/5 = 0.8; low band (==1) grounds 1/5 = 0.2 -> clean separation
    history = ([{"convergence": 2, "grounded": True}] * 4 + [{"convergence": 2, "grounded": False}]
               + [{"convergence": 1, "grounded": True}] + [{"convergence": 1, "grounded": False}] * 4)
    res = convergence(history)
    assert res["gate_on"] is True
    assert res["high_band_rate"] > res["low_band_rate"]
    assert res["margin"] > 0.10
    assert "gate ON" in res["verdict"]


def test_convergence_gate_stays_advisory_on_small_sample():
    # too few samples per band -> the gate stays closed regardless of any incidental separation
    res = convergence([{"convergence": 2, "grounded": True}, {"convergence": 1, "grounded": False}])
    assert res["gate_on"] is False
    assert "advisory" in res["verdict"]


def test_convergence_gate_stays_advisory_on_flat_sample():
    # both bands ground at the SAME rate (0.5) across enough samples -> no separation -> advisory
    history = ([{"convergence": 2, "grounded": True}, {"convergence": 2, "grounded": False}] * 3
               + [{"convergence": 1, "grounded": True}, {"convergence": 1, "grounded": False}] * 3)
    res = convergence(history)
    assert res["gate_on"] is False
    assert res["margin"] == 0.0
    assert "advisory" in res["verdict"]


def test_convergence_gate_tolerates_malformed_rows():
    # a non-dict row / non-numeric convergence must degrade (default 1), never crash the gate
    res = convergence([{"convergence": 2, "grounded": True}, "junk",
                       {"convergence": "x", "grounded": False}])
    assert isinstance(res["gate_on"], bool) and res["n"] == 2  # the "junk" string row is dropped


def test_convergence_cli_smoke(capsys):
    # `python -m kg_engine.harness convergence` runs without arguments (uses the demo history)
    rc = _main(["convergence"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "gate_on" in payload and "verdict" in payload
    assert payload["gate_on"] is True  # the demo separates cleanly -> gate ON
