"""Live KAP ingest loaders.

  seed_companies_from_kap : KAP member list -> Company (+ Security for listed) nodes
  ingest_disclosures      : per-company disclosure METADATA -> Disclosure nodes
                            + HAS_DISCLOSURE edges, with optional raw-doc caching

Scope boundary: this populates IDENTITY and DISCLOSURE METADATA from the live API.
It does NOT extract ownership %s / board rosters — those live inside disclosure
documents and are turned into HOLDS_STAKE / CONTROLS / BOARD_MEMBER_OF / Event
edges by LLM extraction in Phase 3 (architecture §11, steps 5-7).
"""
from __future__ import annotations

from pathlib import Path

import kuzu

from tmkg import config
from tmkg.adapters.kap_adapter import KapAdapter, CompanyRef


def _co_uuid(kap_oid: str) -> str:
    return f"co-{kap_oid.lower()}"


def _se_uuid(kap_oid: str, ticker: str) -> str:
    return f"se-{kap_oid.lower()}-{ticker.lower()}"


def seed_companies_from_kap(
    conn: kuzu.Connection,
    adapter: KapAdapter,
    member_types=("IGS",),
    listed_only: bool = True,
    refresh: bool = False,
) -> dict:
    """Upsert Company nodes (and Security + ISSUES for listed names) from KAP."""
    members = adapter.fetch_members(member_types=member_types, refresh=refresh)
    n_co = n_se = 0
    for m in members:
        if listed_only and not m.is_listed:
            continue
        uuid = _co_uuid(m.kap_oid)
        conn.execute(
            """
            MERGE (c:Company {uuid: $uuid})
            SET c.kap_oid=$kap_oid, c.ticker=$ticker, c.name=$name,
                c.jurisdiction='TR', c.is_listed=$is_listed
            """,
            {"uuid": uuid, "kap_oid": m.kap_oid, "ticker": m.primary_ticker,
             "name": m.name, "is_listed": m.is_listed},
        )
        # stash mkk_oid as registration_authority-adjacent? keep separate: store on Security note
        n_co += 1
        if m.is_listed and m.primary_ticker:
            suid = _se_uuid(m.kap_oid, m.primary_ticker)
            conn.execute(
                """
                MERGE (s:Security {uuid: $suid})
                SET s.ticker=$ticker, s.type='EQUITY', s.currency='TRY'
                """,
                {"suid": suid, "ticker": m.primary_ticker},
            )
            conn.execute(
                """
                MATCH (c:Company {uuid: $uuid}), (s:Security {uuid: $suid})
                MERGE (c)-[:ISSUES]->(s)
                """,
                {"uuid": uuid, "suid": suid},
            )
            n_se += 1
    return {"companies": n_co, "securities": n_se, "members_seen": len(members)}


def ingest_disclosures(
    conn: kuzu.Connection,
    adapter: KapAdapter,
    member: CompanyRef,
    start: str,
    end: str,
    subject_oids=None,
    cache_raw: bool = False,
) -> int:
    """Ingest one company's disclosure metadata for [start, end] (same year)."""
    if not member.mkk_oid:
        return 0
    co_uuid = _co_uuid(member.kap_oid)
    discs = adapter.fetch_disclosures(member.mkk_oid, start, end, subject_oids=subject_oids)
    raw_dir = Path(config.RAW_DOCS_PATH)
    if cache_raw:
        raw_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in discs:
        idx = str(d.index)
        pub = getattr(d, "publish_datetime", None)
        date_str = pub.date().isoformat() if pub else None
        conn.execute(
            """
            MERGE (x:Disclosure {index: $index})
            SET x.subject=$subject, x.disclosure_type=$dtype,
                x.date=CASE WHEN $date IS NULL THEN NULL ELSE date($date) END,
                x.stock_codes=$stock, x.has_attachment=$att, x.url=$url
            """,
            {"index": idx, "subject": d.subject, "dtype": d.disclosure_type,
             "date": date_str, "stock": getattr(d, "stock_codes", None),
             "att": bool(getattr(d, "has_attachment", False)),
             "url": getattr(d, "url", None)},
        )
        conn.execute(
            """
            MATCH (c:Company {uuid: $co}), (x:Disclosure {index: $index})
            MERGE (c)-[:HAS_DISCLOSURE]->(x)
            """,
            {"co": co_uuid, "index": idx},
        )
        if cache_raw:
            fp = raw_dir / f"{idx}.html"
            if not fp.exists():
                try:
                    fp.write_text(adapter.fetch_detail_html(idx), encoding="utf-8")
                except Exception:
                    pass  # raw caching is best-effort; metadata already stored
        n += 1
    return n
