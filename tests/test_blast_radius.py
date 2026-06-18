"""Offline tests for the multi-hop blast-radius analytics.

Builds a synthetic two-level group with debt walls so the traversal,
root-resolution, window-filtering and aggregation are all exercised without
touching the live graph.

    PYTHONPATH=src python -m pytest tests/test_blast_radius.py -v

Group shape (CONTROLS points parent -> child):

    HOLD ──> SUBA ──> SUBA2
         └─> SUBB
    OUT  (uncontrolled, debt-bearing — must NOT leak into HOLD's blast radius)
"""
from __future__ import annotations

import datetime as _dt
import tempfile
from pathlib import Path

from tmkg.graph.connection import connect
from tmkg.schema.ddl import apply_schema
from tmkg.analytics.blast_radius import (
    resolve_group_root, group_members, refinancing_wall, group_blast_radius,
)

D = _dt.date


def _co(conn, uuid, ticker):
    conn.execute(
        "CREATE (:Company {uuid:$u, ticker:$t, name:$n, is_listed:true})",
        {"u": uuid, "t": ticker, "n": f"{ticker} A.S."},
    )


def _controls(conn, a, b):
    conn.execute(
        """MATCH (x:Company {uuid:$a}), (y:Company {uuid:$b})
           CREATE (x)-[:CONTROLS {basis:'test', confidence:1.0,
                    source:'test', extraction_method:'structured'}]->(y)""",
        {"a": a, "b": b},
    )


def _debt(conn, issuer, sid, cls, ccy, maturity, nominal=None):
    conn.execute(
        """CREATE (:Security {uuid:$s, type:$cls, currency:$ccy,
                    maturity_date:$m, maturity_confidence:0.9,
                    nominal:$nom, nominal_currency:$ncc})""",
        {"s": sid, "cls": cls, "ccy": ccy, "m": maturity,
         "nom": nominal, "ncc": (ccy if nominal else None)},
    )
    conn.execute(
        """MATCH (c:Company {uuid:$i}), (s:Security {uuid:$s})
           CREATE (c)-[:ISSUES {instrument_class:$cls, source:'test',
                    extraction_method:'structured', confidence:1.0}]->(s)""",
        {"i": issuer, "s": sid, "cls": cls},
    )


def _build():
    tmp = Path(tempfile.mkdtemp()) / "br.kuzu"
    conn = connect(tmp)
    apply_schema(conn)
    for u, t in [("hold", "HOLD"), ("suba", "SUBA"), ("suba2", "SUBA2"),
                 ("subb", "SUBB"), ("out", "OUT")]:
        _co(conn, u, t)
    _controls(conn, "hold", "suba")
    _controls(conn, "hold", "subb")
    _controls(conn, "suba", "suba2")   # second level
    # Debt walls. Window of interest: 2026-06-01 .. 2027-12-01
    _debt(conn, "suba", "d-suba-1", "BOND", "TRY", D(2026, 9, 1), nominal=100_000_000)   # in, priced
    _debt(conn, "suba", "d-suba-2", "BILL", "TRY", D(2027, 3, 1))                        # in, unpriced
    _debt(conn, "suba2", "d-suba2-1", "EUROBOND", None, D(2026, 12, 1))  # in
    _debt(conn, "subb", "d-subb-1", "SUKUK", "TRY", D(2030, 1, 1))    # OUT of window
    _debt(conn, "hold", "d-hold-1", "BOND", "TRY", D(2027, 1, 1), nominal=50_000_000)    # in, priced
    _debt(conn, "out", "d-out-1", "BOND", "TRY", D(2026, 7, 1))       # in window but OFF-group
    return conn


WIN = (D(2026, 6, 1), D(2027, 12, 1))


def test_root_resolution_from_deep_subsidiary():
    conn = _build()
    roots = resolve_group_root(conn, "suba2")
    assert [r["uuid"] for r in roots] == ["hold"]
    assert roots[0]["hops_from_seed"] == 2


def test_root_of_apex_is_itself():
    conn = _build()
    roots = resolve_group_root(conn, "hold")
    assert [r["uuid"] for r in roots] == ["hold"]
    assert roots[0]["hops_from_seed"] == 0


def test_member_fanout_includes_apex_and_all_levels():
    conn = _build()
    members = {m["uuid"]: m for m in group_members(conn, "hold")}
    assert set(members) == {"hold", "suba", "subb", "suba2"}
    assert members["hold"]["control_hops"] == 0
    assert members["suba"]["control_hops"] == 1
    assert members["suba2"]["control_hops"] == 2


def _controls_basis(conn, a, b, basis):
    conn.execute(
        """MATCH (x:Company {uuid:$a}), (y:Company {uuid:$b})
           CREATE (x)-[:CONTROLS {basis:$basis, confidence:0.95,
                    source:'gleif', extraction_method:'structured'}]->(y)""",
        {"a": a, "b": b, "basis": basis})


def test_control_hops_ignore_ultimate_consolidation_shortcut():
    """GLEIF L2 writes KCHOL->YKB->YKR (direct) AND a KCHOL->YKR ultimate
    shortcut. control_hops must report the real depth (YKB 1, YKR 2), not the
    shortcut's 1. Membership is unchanged — all three are members (F4)."""
    tmp = Path(tempfile.mkdtemp()) / "hops.kuzu"
    conn = connect(tmp)
    apply_schema(conn)
    for u, t in [("kchol", "KCHOL"), ("ykb", "YKB"), ("ykr", "YKR")]:
        _co(conn, u, t)
    _controls_basis(conn, "kchol", "ykb", "direct-consolidation")
    _controls_basis(conn, "ykb", "ykr", "direct-consolidation")
    _controls_basis(conn, "kchol", "ykr", "ultimate-consolidation")   # shortcut
    members = {m["uuid"]: m for m in group_members(conn, "kchol")}
    assert set(members) == {"kchol", "ykb", "ykr"}     # membership intact
    assert members["kchol"]["control_hops"] == 0
    assert members["ykb"]["control_hops"] == 1
    assert members["ykr"]["control_hops"] == 2         # not 1 via the shortcut


def _reports_dir(debt=None, spv=None) -> Path:
    """A temp cache dir holding synthetic run reports for coverage/ingest tests."""
    import json
    d = Path(tempfile.mkdtemp())
    (d / "mkk_debt_report.json").write_text(json.dumps(debt or {}), encoding="utf-8")
    (d / "spv_parent_report.json").write_text(json.dumps(spv or {}), encoding="utf-8")
    return d


def test_coverage_class_blind_for_isolated_seed():
    """A seed alone with no control edge and no priced debt is 'blind': counts
    only, NO money total (the confident-emptiness failure made honest)."""
    tmp = Path(tempfile.mkdtemp()) / "blind.kuzu"
    conn = connect(tmp)
    apply_schema(conn)
    _co(conn, "solo", "SOLO")
    _debt(conn, "solo", "d-solo", "EUROBOND", "FX", D(2026, 9, 1))   # unpriced FX
    rep = group_blast_radius(conn, "solo", *WIN, reports_dir=_reports_dir())
    assert rep["coverage"]["coverage_class"] == "blind"
    assert rep["coverage"]["seed_control_edges"] == 0
    assert rep["coverage"]["banner"] == "counts are meaningful; totals are not"
    gt = rep["group_total"]
    assert gt["instruments"] == 1                       # count still reported
    assert "outstanding_by_currency" not in gt          # no money
    assert "partial_totals" not in gt
    assert "totals_suppressed" in gt


def test_coverage_class_blind_when_seed_flagged_unmatched():
    """A seed whose debt was excluded at ingest (in mkk_debt_report.unmatched) is
    blind even if it happens to sit in a group."""
    conn = _build()
    rd = _reports_dir(debt={
        "reference_securities": 100,
        "summary": {"securities_written": 90},
        "unmatched": [{"issuer_name": "SUBA"}],
    })
    rep = group_blast_radius(conn, "suba", *WIN, reports_dir=rd)
    assert rep["coverage"]["seed_in_unmatched_debt"] is True
    assert rep["coverage"]["coverage_class"] == "blind"


def test_coverage_assembled_reports_money_inline_and_preamble():
    conn = _build()
    rep = group_blast_radius(conn, "hold", *WIN, reports_dir=_reports_dir(debt={
        "reference_securities": 2296, "summary": {"securities_written": 1683},
    }))
    cov = rep["coverage"]
    assert cov["coverage_class"] == "assembled"     # HOLD group priced 50%
    assert "banner" not in cov                       # assembled => no banner
    assert cov["seed_control_edges"] == 2            # hold -> suba, hold -> subb
    # 1.4 ingest-exclusion line read from the report
    assert cov["excluded_at_ingest"] == 613
    assert "excluded at ingest: 613" in cov["excluded_at_ingest_note"]
    # assembled => money inline on group_total
    assert rep["group_total"]["outstanding_by_currency"]["TRY"] == 150_000_000.0


def test_provenance_tier_is_worst_edge_on_path():
    """A member's tier is the weakest edge tier on its best path; confidence is
    the product along it. GLEIF (0.95) > KAP (0.85) > inference (0.70)."""
    from tmkg.analytics.blast_radius import member_provenance
    tmp = Path(tempfile.mkdtemp()) / "prov.kuzu"
    conn = connect(tmp)
    apply_schema(conn)
    for u, t in [("h", "H"), ("g", "G"), ("k", "K"), ("s", "S")]:
        _co(conn, u, t)
    _controls_basis(conn, "h", "g", "direct-consolidation")   # gleif 0.95
    # H -> K via a KAP edge (0.85); K -> S via an inference edge (0.70)
    conn.execute("""MATCH (x:Company{uuid:'h'}),(y:Company{uuid:'k'})
        CREATE (x)-[:CONTROLS {basis:'kap-bagli-ortaklik', confidence:0.85,
        source:'kap', extraction_method:'structured'}]->(y)""")
    conn.execute("""MATCH (x:Company{uuid:'k'}),(y:Company{uuid:'s'})
        CREATE (x)-[:CONTROLS {basis:'spv-naming-convention', confidence:0.70,
        source:'spv', extraction_method:'heuristic'}]->(y)""")
    members = {m["uuid"]: m for m in group_members(conn, "h")}
    assert members["h"]["provenance_tier"] == "group_root"
    assert members["g"]["provenance_tier"] == "gleif_confirmed"
    assert members["g"]["min_path_confidence"] == 0.95
    assert members["k"]["provenance_tier"] == "kap_declared"
    # S's path crosses an inference edge -> worst tier is inference; conf product
    assert members["s"]["provenance_tier"] == "inference_attached"
    assert members["s"]["min_path_confidence"] == round(0.85 * 0.70, 4)
    # direct helper agrees
    prov = member_provenance(conn, "h", ["g", "k", "s"])
    assert prov["s"][0] == "inference_attached"


def test_group_total_splits_by_provenance_tier():
    """group_total.by_provenance_tier buckets each member's wall by its tier so a
    confident figure and an inference-attached figure never merge (F7)."""
    tmp = Path(tempfile.mkdtemp()) / "provg.kuzu"
    conn = connect(tmp)
    apply_schema(conn)
    for u, t in [("h", "H"), ("g", "G"), ("i", "I")]:
        _co(conn, u, t)
    _controls_basis(conn, "h", "g", "direct-consolidation")
    conn.execute("""MATCH (x:Company{uuid:'h'}),(y:Company{uuid:'i'})
        CREATE (x)-[:CONTROLS {basis:'spv-naming-convention', confidence:0.70,
        source:'spv', extraction_method:'heuristic'}]->(y)""")
    _debt(conn, "g", "d-g", "BOND", "TRY", D(2026, 9, 1), nominal=10_000_000)
    _debt(conn, "i", "d-i", "BOND", "TRY", D(2026, 9, 1), nominal=3_000_000)
    rep = group_blast_radius(conn, "h", D(2026, 6, 1), D(2027, 12, 1))
    tiers = rep["group_total"]["by_provenance_tier"]
    assert tiers["gleif_confirmed"]["outstanding_by_currency"]["TRY"] == 10_000_000.0
    assert tiers["inference_attached"]["outstanding_by_currency"]["TRY"] == 3_000_000.0
    assert tiers["gleif_confirmed"]["instruments"] == 1
    assert tiers["inference_attached"]["instruments"] == 1


def test_demote_jv_suspect_edge():
    from tmkg.loaders.spv_parent_backfill import demote_jv_suspect_edges
    from tmkg.analytics.blast_radius import basis_to_tier
    tmp = Path(tempfile.mkdtemp()) / "jv.kuzu"
    conn = connect(tmp)
    apply_schema(conn)
    _co(conn, "kchol", "KCHOL")
    _co(conn, "ksfin", "KSFIN")
    conn.execute("""MATCH (x:Company{uuid:'kchol'}),(y:Company{uuid:'ksfin'})
        CREATE (x)-[:CONTROLS {basis:'spv-naming-convention', confidence:0.70,
        source:'spv', extraction_method:'heuristic'}]->(y)""")
    out = demote_jv_suspect_edges(conn)
    assert out["jv_suspects_demoted"] == 1
    r = conn.execute("MATCH (:Company{ticker:'KCHOL'})-[r:CONTROLS]->"
                     "(:Company{ticker:'KSFIN'}) RETURN r.basis, r.confidence")
    basis, conf = r.get_next()
    assert basis == "spv-naming-convention-jv-suspect" and conf == 0.40
    assert basis_to_tier(basis) == "inference_attached"
    # idempotent / no-op when the edge is absent
    assert demote_jv_suspect_edges(conn, (("NOPE", "NADA"),))["jv_suspects_demoted"] == 0


def test_refinancing_wall_window_filter():
    conn = _build()
    w = refinancing_wall(conn, "suba", *WIN)
    assert w["instruments"] == 2
    assert w["by_class"] == {"BOND": 1, "BILL": 1}
    assert w["earliest_maturity"] == D(2026, 9, 1)
    # subb's only bond matures in 2030 — outside the window
    assert refinancing_wall(conn, "subb", *WIN)["instruments"] == 0


def test_group_blast_radius_aggregates_and_excludes_offgroup():
    conn = _build()
    rep = group_blast_radius(conn, "suba2", *WIN)
    # apex correctly resolved upward from the deep seed
    assert rep["root_used"] == "hold"
    # hold(1) + suba(2) + suba2(1) = 4 instruments; subb=0; OUT excluded
    assert rep["group_total"]["instruments"] == 4
    assert rep["group_total"]["members_with_wall"] == 3
    assert rep["group_total"]["members_total"] == 4
    # off-group issuer OUT must never appear
    assert all(m["uuid"] != "out" for m in rep["members"])
    # currency channel split survives aggregation (EUROBOND has null ccy)
    assert rep["group_total"]["by_currency"].get("TRY") == 3
    assert rep["group_total"]["by_currency"].get("UNKNOWN") == 1
    # seed wall reported on the seed itself
    assert rep["seed"]["ticker"] == "SUBA2"
    assert rep["seed"]["wall"]["instruments"] == 1


def test_members_ranked_by_wall_desc():
    conn = _build()
    rep = group_blast_radius(conn, "hold", *WIN)
    walls = [m["wall"]["instruments"] for m in rep["members"]]
    assert walls == sorted(walls, reverse=True)
    assert rep["members"][0]["uuid"] == "suba"  # 2 instruments, the heaviest


def test_nominal_rollup_and_partial_coverage():
    conn = _build()
    rep = group_blast_radius(conn, "hold", *WIN)
    gt = rep["group_total"]
    # priced: suba(100m) + hold(50m) = 150m TRY across the 4-instrument wall
    assert gt["outstanding_by_currency"]["TRY"] == 150_000_000.0
    assert gt["nominal_by_currency"]["TRY"] == 150_000_000.0  # back-compat alias
    assert gt["priced_instruments"] == 2
    assert gt["instruments"] == 4
    assert gt["nominal_coverage"] == 0.5   # only half the wall carries a nominal
    # per-member coverage surfaces too
    suba = next(m for m in rep["members"] if m["uuid"] == "suba")
    assert suba["wall"]["outstanding_by_currency"]["TRY"] == 100_000_000.0
    assert suba["wall"]["nominal_coverage"] == 0.5  # 1 of suba's 2 priced


def test_as_of_rolls_off_matured_debt():
    """Querying with as_of AFTER an instrument matures drops it to 0 outstanding,
    with no graph mutation — the staleness fix for roll-off."""
    conn = _build()
    # suba's bond matures 2026-09-01. As of 2026-10-01 it has rolled off.
    rep = group_blast_radius(conn, "hold", D(2026, 6, 1), D(2027, 12, 1),
                             as_of=D(2026, 10, 1))
    suba = next(m for m in rep["members"] if m["uuid"] == "suba")
    # the matured 100m bond no longer counts as outstanding
    assert suba["wall"]["outstanding_by_currency"].get("TRY", 0) == 0.0
    assert suba["wall"]["matured_in_window"] == 1
    # rolling off the only big priced bond drops coverage to 25% -> 'partial':
    # the total moves under partial_totals, never a headline (refusal logic).
    assert rep["coverage"]["coverage_class"] == "partial"
    gt = rep["group_total"]
    assert "outstanding_by_currency" not in gt          # not presented as headline
    assert gt["partial_totals"]["outstanding_by_currency"]["TRY"] == 50_000_000.0
    assert gt["partial_totals"]["instruments_unpriced"] == 3
    assert gt["matured_in_window"] == 1                  # counts stay at top level


def test_amortizing_instrument_is_flagged_not_summed_confidently():
    """An amortizing priced instrument must land in the upper-bound bucket, never
    inflate the confident outstanding total."""
    conn = _build()
    # mark suba's bond as amortizing
    conn.execute("MATCH (s:Security {uuid:'d-suba-1'}) SET s.is_amortizing = true")
    rep = group_blast_radius(conn, "hold", *WIN)
    gt = rep["group_total"]
    # confident now only hold's 50m bullet; suba's 100m is an upper bound
    assert gt["outstanding_by_currency"]["TRY"] == 50_000_000.0
    assert gt["outstanding_upper_by_currency"]["TRY"] == 100_000_000.0
    assert gt["priced_instruments"] == 2  # still counts as priced coverage
