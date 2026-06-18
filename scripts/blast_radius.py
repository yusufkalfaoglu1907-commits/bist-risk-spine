#!/usr/bin/env python3
"""Multi-hop blast-radius query: group contagion from one subsidiary's wall.

Given a seed company (by ticker or uuid) and a maturity window, resolves the
seed's controlling group via CONTROLS, fans out to every group member, and
totals each member's refinancing wall inside the window. See
`tmkg.analytics.blast_radius` for the mechanics and its two limitations
(counts not money; control-graph coverage gaps).

Examples:
    # Koç finance arm, debt maturing in the next 18 months
    PYTHONPATH=src python scripts/blast_radius.py --db ./data/tmkg.kuzu \
        --seed KOCFN --months 18

    # explicit window, by uuid, deeper traversal
    PYTHONPATH=src python scripts/blast_radius.py --db ./data/tmkg.kuzu \
        --seed YKFIN --from 2026-06-01 --to 2027-12-31 --max-hops 6

    # JSON out for downstream tooling
    PYTHONPATH=src python scripts/blast_radius.py --db ./data/tmkg.kuzu \
        --seed KOCFN --months 18 --json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json

from tmkg.graph.connection import connect
from tmkg.analytics.blast_radius import group_blast_radius


def _resolve_seed(conn, seed: str) -> str:
    """Accept a ticker or a uuid; return the company uuid."""
    r = conn.execute("MATCH (c:Company {uuid:$s}) RETURN c.uuid", {"s": seed})
    if r.has_next():
        return r.get_next()[0]
    r = conn.execute("MATCH (c:Company {ticker:$t}) RETURN c.uuid", {"t": seed})
    if r.has_next():
        return r.get_next()[0]
    raise SystemExit(f"no company with ticker or uuid {seed!r}")


def _fmt_money(by_ccy: dict) -> str:
    if not by_ccy:
        return ""
    parts = []
    for k, v in sorted(by_ccy.items()):
        if v >= 1e9:
            parts.append(f"{v/1e9:.2f}bn {k}")
        elif v >= 1e6:
            parts.append(f"{v/1e6:.1f}m {k}")
        else:
            parts.append(f"{v:,.0f} {k}")
    return ", ".join(parts)


def _fmt_wall(w: dict) -> str:
    if not w["instruments"]:
        return "—"
    cls = ", ".join(f"{k}:{v}" for k, v in sorted(w["by_class"].items()))
    first = w["earliest_maturity"]
    money = _fmt_money(w["outstanding_by_currency"])
    money = f"  ≈{money}" if money else ""
    upper = _fmt_money(w.get("outstanding_upper_by_currency", {}))
    upper = f"  (+≤{upper} amort.)" if upper else ""
    return f"{w['instruments']:>3}  ({cls})  first={first}{money}{upper}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--seed", required=True, help="seed company ticker or uuid")
    ap.add_argument("--months", type=int, default=18,
                    help="window length in months from --from (default 18)")
    ap.add_argument("--from", dest="start", default=None,
                    help="window start YYYY-MM-DD (default: today)")
    ap.add_argument("--to", dest="end", default=None,
                    help="window end YYYY-MM-DD (overrides --months)")
    ap.add_argument("--max-hops", type=int, default=6)
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="compute outstanding as of this date YYYY-MM-DD "
                         "(default: today). Matured paper auto-drops to 0.")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()
    as_of = _dt.date.fromisoformat(args.as_of) if args.as_of else _dt.date.today()

    start = _dt.date.fromisoformat(args.start) if args.start else _dt.date.today()
    if args.end:
        end = _dt.date.fromisoformat(args.end)
    else:
        m = start.month - 1 + args.months
        end = start.replace(year=start.year + m // 12, month=m % 12 + 1)

    conn = connect(args.db)
    seed_uuid = _resolve_seed(conn, args.seed)
    report = group_blast_radius(conn, seed_uuid, start, end, args.max_hops, as_of=as_of)

    if args.json:
        def _default(o):
            if isinstance(o, _dt.date):
                return o.isoformat()
            raise TypeError
        print(json.dumps(report, default=_default, ensure_ascii=False, indent=2))
        return

    s = report["seed"]
    gt = report["group_total"]
    cov = report["coverage"]
    print(f"\nSEED  {s['ticker']}  {s['name']}")
    print(f"      own wall {start}..{end}:  {_fmt_wall(s['wall'])}")
    roots = report["roots"]
    if roots:
        rstr = "; ".join(f"{r['ticker']} (+{r['hops_from_seed']})" for r in roots)
        print(f"APEX  {rstr}    [fan-out rooted at {report['root_used']}]")

    # Coverage preamble + refusal banner (F1/F2/F5).
    print(f"\nCOVERAGE  class={cov['coverage_class']}  "
          f"seed_control_edges={cov['seed_control_edges']}  "
          f"priced={cov['nominal_coverage']:.0%} "
          f"({cov['instruments_priced']}/{cov['instruments_priced']+cov['instruments_unpriced']})")
    if cov.get("seed_in_unmatched_debt"):
        print("  ⚠ seed's own debt was EXCLUDED at ingest (mkk_debt_report.unmatched)")
    if cov.get("seed_in_no_in_graph_parent"):
        print("  ⚠ seed has NO in-graph parent (spv_parent_report.no_in_graph_parent)")
    if cov.get("excluded_at_ingest_note"):
        print(f"  {cov['excluded_at_ingest_note']}")
    if cov.get("banner"):
        print(f"  ** {cov['banner']} **")

    print(f"\nGROUP CONTAGION  ({gt['members_with_wall']}/{gt['members_total']} members carry a wall)")
    print(f"  total instruments maturing in window: {gt['instruments']}")
    if gt["by_class"]:
        print("  by class:    " + ", ".join(f"{k}:{v}" for k, v in sorted(gt['by_class'].items())))
        print("  by currency: " + ", ".join(f"{k}:{v}" for k, v in sorted(gt['by_currency'].items())))

    # Money: inline when assembled, under partial_totals when partial, withheld
    # when blind.
    if cov["coverage_class"] == "blind":
        print(f"  outstanding: WITHHELD — {gt['totals_suppressed']}")
    else:
        src = gt if cov["coverage_class"] == "assembled" else gt["partial_totals"]
        money = _fmt_money(src["outstanding_by_currency"])
        upper = _fmt_money(src.get("outstanding_upper_by_currency", {}))
        label = "outstanding" if cov["coverage_class"] == "assembled" else "PARTIAL total (not a headline figure)"
        if money or upper:
            line = f"  {label} as of {as_of} (bullet, confident): ≈{money or '0'}"
            if upper:
                line += f"  | +≤{upper} amortizing (upper bound)"
            print(line)
            print(f"    [priced {src['priced_instruments']}/{gt['instruments']} = "
                  f"{src['nominal_coverage']:.0%}; matured/rolled-off: {gt['matured_in_window']}]")
            prov = src.get("by_provenance_tier") or {}
            for tier in ("group_root", "gleif_confirmed", "kap_declared",
                         "inference_attached", "unknown"):
                if tier in prov:
                    tm = _fmt_money(prov[tier]["outstanding_by_currency"])
                    print(f"      {tier:18} {prov[tier]['instruments']:>3} instr"
                          f"{('  ≈' + tm) if tm else ''}")
        else:
            print("  outstanding: none priced yet — run extract_kap_nominals.py + --stage nominal")

    print(f"\n  {'hops':>4}  {'ticker':<8} {'tier':<12} {'wall':<40} name")
    for m in report["members"]:
        flag = " *" if m["uuid"] == s["uuid"] else "  "
        print(f"  {m['control_hops']:>4}{flag}{(m['ticker'] or '?'):<8} "
              f"{(m.get('provenance_tier') or '?'):<12} "
              f"{_fmt_wall(m['wall']):<40} {m['name']}")
    print()


if __name__ == "__main__":
    main()
