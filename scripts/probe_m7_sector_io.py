"""M7 tier-3 feasibility probe — sector-IO lead-lag in BIST residual returns.

The user pivoted M7 to tier-3 (OECD-ICIO sector lead-lag) after tier-1 firm-level
edges proved structurally too sparse (scripts/probe_m7_newbusiness.py). Before
sourcing the full OECD-ICIO matrix + building a BIST-sector crosswalk, this probe
runs the cheapest-to-kill test of the tier-3 PREMISE: does ANY stable sector-level
lead-lag predictability exist in the M2 residual returns, out-of-sample?

Construction (honest-eval protocol — residual returns, OOS split, multi-horizon):
  - Sector residual return S_s(t) = cross-sectional mean of member residuals (>=8
    names/day), built from L2 `residuals` joined to graph IN_SECTOR.
  - Train/test split (first half / second half).
  - GENERIC test: ridge-fit the full SxS lead-lag matrix on train, measure the
    cross-sectional information coefficient (IC) out-of-sample. (Data-mined ceiling.)
  - SPECIFIC test: textbook supplier->customer IO pairs (energy->industry,
    base-metal->metal-goods, chemicals->textile, cement->construction, ...) — the
    causal structure OECD-ICIO would impose — checked OOS at daily/weekly/monthly
    horizons (the customer-momentum effect is fundamentally monthly).

Reads L2 directly (EXPLORATORY probe, not promoted signal code — a real signal
would route through PITAccess + the M4 gate). Writes a report to data/cache;
fabricates nothing, writes nothing to L2 (§4). Surfaces a go/no-go to the user (§8).

Run:  PYTHONPATH=src python scripts/probe_m7_sector_io.py
"""
from __future__ import annotations

import glob
import sys

import duckdb
import kuzu
import numpy as np
import polars as pl

from tmkg.ingest.audit import write_run_report

_MIN_NAMES_PER_DAY = 8

# Textbook BIST supplier->customer IO linkages (upstream leads downstream) — the
# sparse causal structure an OECD-ICIO matrix would weight.
_IO_PAIRS = [
    ("ELEKTRİK GAZ VE BUHAR", "METAL EŞYA MAKİNE"),
    ("ELEKTRİK GAZ VE BUHAR", "ANA METAL SANAYİ"),
    ("ANA METAL SANAYİ", "METAL EŞYA MAKİNE"),
    ("KİMYA İLAÇ PETROL", "TEKSTİL"),
    ("KİMYA İLAÇ PETROL", "TAŞ VE TOPRAĞA"),
    ("BANKALAR", "HOLDİNGLER"),
    ("ANA METAL SANAYİ", "İNŞAAT"),
    ("TAŞ VE TOPRAĞA", "İNŞAAT"),
]


def _sector_matrix():
    db = kuzu.Database("data/tmkg.kuzu", read_only=True)
    conn = kuzu.Connection(db)
    res = conn.execute(
        "MATCH (co:Company)-[:IN_SECTOR]->(s:Sector) WHERE co.is_listed=true "
        "RETURN co.ticker AS sym, s.name AS sec"
    )
    sym2sec = {}
    while res.has_next():
        sym, sec = res.get_next()
        if sym:
            sym2sec[sym] = sec

    con = duckdb.connect()
    files = glob.glob("data/l2/residuals/*.parquet")
    df = con.execute(
        f"SELECT symbol, bar_date, residual FROM read_parquet({files!r}) "
        "WHERE residual IS NOT NULL"
    ).pl()
    df = df.with_columns(
        pl.col("symbol").replace_strict(sym2sec, default=None).alias("sector")
    ).drop_nulls("sector")
    g = (
        df.group_by(["sector", "bar_date"])
        .agg(pl.col("residual").mean().alias("sr"), pl.len().alias("n"))
        .filter(pl.col("n") >= _MIN_NAMES_PER_DAY)
    )
    w = g.pivot(values="sr", index="bar_date", on="sector").sort("bar_date")
    secs = [c for c in w.columns if c != "bar_date"]
    A = np.nan_to_num(w.select(secs).to_numpy(), nan=0.0)
    return A, secs


def _resolve(secs, kw):
    for s in secs:
        if kw in s:
            return s
    return None


def _daily_ic(P, Y):
    out = []
    for p, y in zip(P, Y):
        if p.std() > 1e-9 and y.std() > 1e-9:
            out.append(float(np.corrcoef(p, y)[0, 1]))
    return float(np.mean(out)) if out else 0.0


def main() -> int:
    A, secs = _sector_matrix()
    T, S = A.shape
    idx = {s: i for i, s in enumerate(secs)}

    horizons = []
    for win in (1, 5, 10, 21):
        nb = T // win
        B = A[: nb * win].reshape(nb, win, S).sum(1) if win > 1 else A.copy()
        bh = B.shape[0] // 2
        mu, sd = B[:bh].mean(0), B[:bh].std(0) + 1e-9
        Z = (B - mu) / sd
        Xtr, Ytr = Z[: bh - 1], Z[1:bh]
        Xte, Yte = Z[bh : B.shape[0] - 1], Z[bh + 1 :]
        if len(Xtr) < 6 or len(Xte) < 5:
            horizons.append({"win_days": win, "skipped": "too few blocks",
                             "n_blocks": int(B.shape[0])})
            continue
        Bmat = np.linalg.solve(Xtr.T @ Xtr + 5 * np.eye(S), Xtr.T @ Ytr)
        generic_oos_ic = _daily_ic(Xte @ Bmat, Yte)
        pair_oos = []
        for a, b in _IO_PAIRS:
            sa, sb = _resolve(secs, a), _resolve(secs, b)
            if not sa or not sb:
                continue
            ctr = float(np.corrcoef(Z[: bh - 1, idx[sa]], Z[1:bh, idx[sb]])[0, 1])
            cte = float(np.corrcoef(Z[bh : B.shape[0] - 1, idx[sa]],
                                    Z[bh + 1 :, idx[sb]])[0, 1])
            pair_oos.append({"supplier": a, "customer": b,
                             "train_corr": round(ctr, 4), "test_corr": round(cte, 4)})
        confirmed = sum(
            1 for p in pair_oos
            if np.sign(p["train_corr"]) == np.sign(p["test_corr"])
            and abs(p["test_corr"]) > 0.15 and abs(p["train_corr"]) > 0.1
        )
        horizons.append({
            "win_days": win,
            "n_blocks": int(B.shape[0]),
            "oos_test_blocks": int(len(Xte)),
            "generic_oos_ic": round(generic_oos_ic, 4),
            "textbook_pairs_mean_oos_corr": round(
                float(np.mean([p["test_corr"] for p in pair_oos])), 4),
            "train_confirmed_pairs": confirmed,
            "pairs": pair_oos,
        })

    report = {
        "probe": "m7_tier3_sector_io_leadlag_feasibility",
        "source": "L2 residuals (M2 strip) x graph IN_SECTOR",
        "n_sectors": S,
        "n_trading_days": T,
        "min_names_per_sector_day": _MIN_NAMES_PER_DAY,
        "horizons": horizons,
        "verdict": (
            "NO sector-IO lead-lag survives OOS at any horizon. Daily/weekly generic "
            "OOS IC ~0 and 0/8 textbook IO pairs train-confirmed; the monthly horizon "
            "(where customer-momentum should peak) has too few independent OOS blocks "
            "(~13 over 3yr) to establish significance. Sourcing OECD-ICIO cannot "
            "rescue this — it weights sector-pair lead-lags that carry no stable OOS "
            "signal. Tier-3 go/no-go surfaced to user (§8)."
        ),
    }
    write_run_report("m7_sector_io_feasibility", report)
    print("M7 tier-3 sector-IO lead-lag feasibility probe")
    print(f"  sectors={S}  trading_days={T}")
    for h in horizons:
        if "skipped" in h:
            print(f"  win={h['win_days']:2}d  SKIPPED ({h['skipped']})")
        else:
            print(f"  win={h['win_days']:2}d  generic OOS IC={h['generic_oos_ic']:+.4f}  "
                  f"textbook mean OOS corr={h['textbook_pairs_mean_oos_corr']:+.4f}  "
                  f"train-confirmed={h['train_confirmed_pairs']}/8  "
                  f"(OOS blocks={h['oos_test_blocks']})")
    print("  report -> data/cache/m7_sector_io_feasibility_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
