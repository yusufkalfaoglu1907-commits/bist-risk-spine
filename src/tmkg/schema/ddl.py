"""KuzuDB DDL for the Phase-1 subset of the v0.1 ontology.

Phase-1 nodes:  Company, Person, Security, Sector, Portfolio
Phase-1 edges:  HOLDS_STAKE, CONTROLS, SUBSIDIARY_OF, BOARD_MEMBER_OF,
                EXECUTIVE_OF, ISSUES, IN_SECTOR, HOLDS

Later-phase node/edge tables (Disclosure, Event, Regulation, MacroSeries, ...)
are created too so the schema matches the ontology, but Phase-1 loaders only
populate the subset above.

Provenance convention: edges asserted from KAP/LLM carry
    source STRING, extraction_method STRING, confidence DOUBLE
Structured edges set extraction_method='structured', confidence=1.0.
"""
from __future__ import annotations

import kuzu

# --- Node tables -----------------------------------------------------------

NODE_TABLES = [
    """
    CREATE NODE TABLE IF NOT EXISTS Company(
        uuid STRING,
        lei STRING,
        isin STRING,
        kap_oid STRING,
        ticker STRING,
        name STRING,
        legal_form STRING,
        jurisdiction STRING,
        registration_authority STRING,
        is_listed BOOLEAN,
        listing_status STRING,
        is_pep BOOLEAN,
        is_sanctioned BOOLEAN,
        PRIMARY KEY (uuid)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Person(
        uuid STRING,
        name STRING,
        is_pep BOOLEAN,
        is_sanctioned BOOLEAN,
        PRIMARY KEY (uuid)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Security(
        uuid STRING,
        isin STRING,
        ticker STRING,
        type STRING,
        currency STRING,
        issuer_name STRING,
        description STRING,
        maturity_date DATE,
        maturity_confidence DOUBLE,
        nominal DOUBLE,
        nominal_currency STRING,
        nominal_source STRING,
        nominal_as_of DATE,
        nominal_confidence DOUBLE,
        nominal_basis STRING,
        is_amortizing BOOLEAN,
        issue_date DATE,
        PRIMARY KEY (uuid)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Sector(
        code STRING,
        name STRING,
        level INT64,
        parent_code STRING,
        PRIMARY KEY (code)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Portfolio(
        uuid STRING,
        name STRING,
        PRIMARY KEY (uuid)
    )
    """,
    # ---- later-phase nodes (defined now, populated later) ----
    """
    CREATE NODE TABLE IF NOT EXISTS Disclosure(
        index STRING,
        subject STRING,
        disclosure_type STRING,
        date DATE,
        stock_codes STRING,
        has_attachment BOOLEAN,
        url STRING,
        PRIMARY KEY (index)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Event(
        uuid STRING,
        type STRING,
        date DATE,
        description STRING,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE,
        PRIMARY KEY (uuid)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Regulation(
        uuid STRING,
        type STRING,
        title STRING,
        ref STRING,
        date DATE,
        PRIMARY KEY (uuid)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS MacroSeries(
        evds_code STRING,
        name STRING,
        frequency STRING,
        unit STRING,
        PRIMARY KEY (evds_code)
    )
    """,
]

# --- Relationship tables ---------------------------------------------------
# Kuzu supports multiple FROM-TO pairs in one REL table.

REL_TABLES = [
    """
    CREATE REL TABLE IF NOT EXISTS HOLDS_STAKE(
        FROM Person TO Company,
        FROM Company TO Company,
        pct DOUBLE,
        as_of DATE,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS CONTROLS(
        FROM Person TO Company,
        FROM Company TO Company,
        basis STRING,
        as_of DATE,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS SUBSIDIARY_OF(
        FROM Company TO Company,
        as_of DATE,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS BOARD_MEMBER_OF(
        FROM Person TO Company,
        role STRING,
        since DATE,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS EXECUTIVE_OF(
        FROM Person TO Company,
        title STRING,
        since DATE,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS ISSUES(
        FROM Company TO Security,
        instrument_class STRING,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS IN_SECTOR(
        FROM Company TO Sector,
        sector_basis STRING,
        source STRING,
        as_of DATE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS SUBSECTOR_OF(
        FROM Sector TO Sector
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS HOLDS(
        FROM Portfolio TO Security,
        weight DOUBLE,
        qty DOUBLE
    )
    """,
    # ---- later-phase edges ----
    """
    CREATE REL TABLE IF NOT EXISTS HAS_DISCLOSURE(
        FROM Company TO Disclosure
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS ABOUT(
        FROM Event TO Company,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS FROM_DISCLOSURE(
        FROM Event TO Disclosure,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS SUBJECT_TO(
        FROM Company TO Regulation,
        FROM Sector TO Regulation,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS SENSITIVE_TO(
        FROM Company TO MacroSeries,
        FROM Sector TO MacroSeries,
        beta DOUBLE,
        direction STRING
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS ASSOCIATE_OF(
        FROM Person TO Person,
        FROM Person TO Company,
        source STRING,
        extraction_method STRING,
        confidence DOUBLE
    )
    """,
]


# Idempotent column additions for DBs created before a column existed.
# (CREATE TABLE IF NOT EXISTS won't add columns to an existing table.)
_MIGRATIONS = [
    "ALTER TABLE Company ADD listing_status STRING",
    # DORMANT (pre-pillar cleanup 2026-06-18): the debt/nominal Security columns
    # and the ISSUES instrument_class below are no longer populated — the debt
    # subsystem that wrote them is archived (archive/debt-subsystem-2026-06-18.zip).
    # Kept as additive, unpopulated columns to avoid a risky migration; harmless on
    # the equity-only graph and ready if a "credit-shock" event type revives them.
    # Debt-instrument stage: richer Security attributes + ISSUES provenance.
    "ALTER TABLE Security ADD issuer_name STRING",
    "ALTER TABLE Security ADD description STRING",
    "ALTER TABLE Security ADD maturity_date DATE",
    "ALTER TABLE Security ADD maturity_confidence DOUBLE",
    # Nominal/face-value stage: issued amount + provenance, keyed onto Security by ISIN.
    "ALTER TABLE Security ADD nominal DOUBLE",
    "ALTER TABLE Security ADD nominal_currency STRING",
    "ALTER TABLE Security ADD nominal_source STRING",
    "ALTER TABLE Security ADD nominal_as_of DATE",
    "ALTER TABLE Security ADD nominal_confidence DOUBLE",
    # FX eurobond (XS) pricing stage: the nominal is an ISSUE-size upper bound in a
    # native currency, never a confident total. `nominal_basis` distinguishes it
    # ('fx-issue-size-upper-bound') from a confidently-priced TL bullet (NULL).
    "ALTER TABLE Security ADD nominal_basis STRING",
    # Outstanding model: bullet vs amortizing (so outstanding can be computed
    # as-of any date from stored fields, without re-fetching).
    "ALTER TABLE Security ADD is_amortizing BOOLEAN",
    "ALTER TABLE Security ADD issue_date DATE",
    "ALTER TABLE ISSUES ADD instrument_class STRING",
    "ALTER TABLE ISSUES ADD source STRING",
    "ALTER TABLE ISSUES ADD extraction_method STRING",
    "ALTER TABLE ISSUES ADD confidence DOUBLE",
    # Sector stage: two-level KAP taxonomy (main sector / sub-sector + hierarchy).
    "ALTER TABLE Sector ADD level INT64",
    "ALTER TABLE Sector ADD parent_code STRING",
    # Sector inheritance (F8): provenance on IN_SECTOR so a KAP-assigned sector
    # ('kap-direct') is distinguishable from one propagated over CONTROLS
    # ('inherited-from-parent') and never silently overwritten.
    "ALTER TABLE IN_SECTOR ADD sector_basis STRING",
    "ALTER TABLE IN_SECTOR ADD source STRING",
    "ALTER TABLE IN_SECTOR ADD as_of DATE",
]


def apply_schema(conn: kuzu.Connection) -> None:
    """Create all node and rel tables (idempotent via IF NOT EXISTS) and apply
    additive column migrations (idempotent: re-adding an existing column is a
    no-op here, the error is swallowed)."""
    for stmt in NODE_TABLES + REL_TABLES:
        conn.execute(stmt)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except RuntimeError:
            pass  # column already exists
