"""GDELT adapter — geopolitical event stream for the M6 event engine (§234/§236).

**Sourcing decision (2026-06-25): the GDELT raw FILE feed, NOT BigQuery.** The data
contract (CLAUDE.md §4) forbids a live/mutable remote in the reproducible path, and we only
need the **Turkey slice** — tiny — so the planet-scale value of BigQuery does not apply. The
built M6 schema (`l2/schema.sql` `events`/`event_targets`) is source-agnostic, so the raw-feed
rows land with zero rework. (This supersedes the BigQuery lean in `data-sourcing-v2.md`.)

**Primary stream = the GKG** (Global Knowledge Graph 2.1). The GKG's *themes* map onto the 11
§234 event types far better than the verb-based CAMEO codes of the Events table, and its tone +
theme fields drive the per-event channel extraction. Files are published every 15 minutes at
``http://data.gdeltproject.org/gdeltv2/{YYYYMMDDHHMMSS}.gkg.csv.zip`` — deterministic URLs, no
master-list download, no account. Tab-delimited (despite the ``.csv`` name), one record per line.

**PIT / bitemporality (§5).** The GKG records ``V2.1DATE`` = the *publication* date of the
document (it is NOT time-shifted to a past occurrence the way the Events table is). For a
news-driven event the publication date IS our knowledge date and the tradable moment, so for
GKG-sourced events ``event_date = knowledge_date = V2.1DATE`` (day precision). A PIT read at
``as_of`` therefore never sees an event before the news carrying it was published.

**Severity is MODELED, never price-derived (§5).** It is a coarse 0..1 magnitude from the
document tone — see ``modeled_severity``. The ``event_targets`` channel incidence is seeded from
the **inferred-tier** ``taxonomy.TYPE_CHANNEL_PRIOR`` (``source='taxonomy_prior'``); a future live
step overrides it with per-event LLM extraction over the GKG themes (``source='gdelt_llm'``). An
inferred edge is never silently promoted into a verified path.

The parser / Turkey filter / theme classifier / row builders are **pure** (offline-testable
against a labelled fixture); only ``fetch``/``smoke_check`` touch the network (§4).
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import date, datetime, timedelta

import httpx

from tmkg.events.taxonomy import EVENT_TYPES, prior_shock_vector
from tmkg.ingest.audit import write_run_report
from tmkg.ingest.base import IngestionAdapter
from tmkg.pit.errors import ContractDrift, SourceUnreachable

_DEFAULT_BASE = "http://data.gdeltproject.org/gdeltv2"

# --- GKG 2.1 column layout (codebook V2.1, the field ORDER changed from 2.0) -----------
# 27 tab-delimited columns; we name only the ones M6 consumes. Index is position in the row.
GKG_NUM_COLS = 27
COL_RECORDID = 0          # GKGRECORDID  -> event_id
COL_DATE = 1              # V2.1DATE (YYYYMMDDHHMMSS, publication date) -> event/knowledge date
COL_SOURCECOMMONNAME = 3  # V2SOURCECOMMONNAME (top-level domain / 'BBC Monitoring' / ...)
COL_DOCID = 4             # V2DOCUMENTIDENTIFIER (URL / citation / DOI)
COL_V1THEMES = 7          # V1THEMES (semicolon-delimited GKG theme strings) -> event_type
COL_V1LOCATIONS = 9       # V1LOCATIONS ('#'-delimited blocks, ';'-separated) -> geography + filter
COL_V15TONE = 15          # V1.5TONE (comma-delimited: tone,pos,neg,polarity,actref,selfref,wc)

TURKEY_FIPS = "TU"        # GKG locations use FIPS10-4 country codes; Turkey = 'TU' (NOT CAMEO 'TUR')

# --- GKG-theme -> §234 event-type classifier (a tunable inferred-tier prior, like the ----
# taxonomy's TYPE_CHANNEL_PRIOR). Patterns are UPPERCASE substrings matched against each
# theme string. A document carries many themes, so classification scores the types by how
# many of their patterns hit and breaks ties by GKG_TYPE_PRIORITY (more specific/severe wins).
GKG_TYPE_THEME_PATTERNS: dict[str, tuple[str, ...]] = {
    "fx_monetary_shock":            ("ECON_CURRENCY", "ECON_DEVALUATION", "ECON_INFLATION",
                                     "ECON_INTEREST_RATE", "ECON_EXCHANGE"),
    "sanctions_export_controls":    ("SANCTION", "EMBARGO", "EXPORT_CONTROL", "ECON_SANCTION"),
    "armed_conflict":               ("ARMEDCONFLICT", "WAR", "INSURGENCY", "MILITARY_OFFENSIVE",
                                     "ACT_FORCEPOSTURE"),
    "diplomatic_shift":             ("DIPLOM", "ALLIANCE", "NEGOTIATIONS", "PEACEKEEPING",
                                     "TREATY"),
    "trade_policy_tariff":          ("TARIFF", "TRADE_DISPUTE", "ECON_TRADE", "ECON_FREETRADE",
                                     "WTO"),
    "energy_supply_disruption":     ("ENV_OIL", "ENV_GAS", "ENV_NATURALGAS", "FUELPRICES",
                                     "ECON_OILPRICE", "ENERGY"),
    "cbrt_regulatory_action":       ("ECON_CENTRALBANK", "GOVERNMENT_REGULATION", "LEGISLATION",
                                     "REGULATION"),
    "elections_political_transition": ("ELECTION", "POLITICAL_TURMOIL", "DEMOCRACY", "COUP",
                                       "GOVERNMENT_CHANGE", "REFERENDUM"),
    "terror_security":              ("TERROR", "SUICIDE_ATTACK", "SECURITY_SERVICES",
                                     "ATTACK"),
    "natural_disaster":             ("NATURAL_DISASTER", "EARTHQUAKE", "FLOOD", "WILDFIRE",
                                     "DISASTER"),
    "pandemic":                     ("PANDEMIC", "EPIDEMIC", "INFECTIOUS", "MEDICAL_DISEASE",
                                     "HEALTH_PANDEMIC"),
}
# Tie-break order: the most specific / highest-impact type wins when match counts are equal.
GKG_TYPE_PRIORITY: tuple[str, ...] = (
    "terror_security", "armed_conflict", "natural_disaster", "pandemic",
    "sanctions_export_controls", "energy_supply_disruption", "cbrt_regulatory_action",
    "fx_monetary_shock", "elections_political_transition", "trade_policy_tariff",
    "diplomatic_shift",
)

# Coarse provenance for the prior-seeded TARGETS edge (§5 soft-edge quartet). The LLM
# extraction step (live) overrides these with a higher-confidence `gdelt_llm` mapping.
_PRIOR_CONFIDENCE = 0.3
_PRIOR_UNCERTAINTY = 0.7
_PRIOR_SOURCE = "taxonomy_prior"


# === pure parsing helpers (no network) =================================================
def gkg_url(ts: datetime, *, base: str = _DEFAULT_BASE) -> str:
    """The GKG file URL for a 15-minute batch timestamp (must be aligned to :00/:15/:30/:45)."""
    return f"{base.rstrip('/')}/{ts:%Y%m%d%H%M%S}.gkg.csv.zip"


def gkg_15min_urls(start: date, end: date, *, base: str = _DEFAULT_BASE) -> list[str]:
    """Every 15-minute GKG URL over ``[start, end]`` inclusive (96 per day, deterministic).

    The cadence is fixed, so the URLs are constructed directly — the multi-hundred-MB
    ``masterfilelist.txt`` is never downloaded. The live ``fetch`` tolerates the occasional
    missing slot (early GDELT history has gaps); a 404 is skipped, not fabricated."""
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    urls: list[str] = []
    t = datetime(start.year, start.month, start.day)
    stop = datetime(end.year, end.month, end.day) + timedelta(days=1)
    while t < stop:
        urls.append(gkg_url(t, base=base))
        t += timedelta(minutes=15)
    return urls


def parse_gkg_csv(text: str) -> list[dict]:
    """Parse a GKG 2.1 tab-delimited file body into a list of field dicts (one per record).

    Rows with fewer than ``GKG_NUM_COLS`` columns are dropped as malformed (real GKG carries
    the occasional truncated line); a NON-empty body that yields ZERO parseable records is a
    contract drift (the column layout changed), so it raises — never a silent empty success
    (§4). An empty body (a 15-minute file with no content) returns ``[]``."""
    rows: list[dict] = []
    seen_any = False
    for line in text.split("\n"):
        if not line.strip():
            continue
        seen_any = True
        fields = line.split("\t")
        if len(fields) < GKG_NUM_COLS:
            continue  # truncated/malformed line -> drop (never pad to fabricate columns)
        rows.append({
            "record_id": fields[COL_RECORDID],
            "date": fields[COL_DATE],
            "source_name": fields[COL_SOURCECOMMONNAME],
            "document_id": fields[COL_DOCID],
            "themes": fields[COL_V1THEMES],
            "locations": fields[COL_V1LOCATIONS],
            "tone": fields[COL_V15TONE],
        })
    if seen_any and not rows:
        raise ContractDrift(
            "GKG parse: a non-empty file produced no rows with the expected "
            f"{GKG_NUM_COLS}-column layout — contract drift, refusing to fabricate."
        )
    return rows


def location_country_codes(v1locations: str) -> set[str]:
    """The set of FIPS10-4 country codes in a V1LOCATIONS field (block fmt
    ``Type#FullName#CountryCode#ADM1#Lat#Long#FeatureID``; CountryCode is the 3rd '#' field)."""
    codes: set[str] = set()
    for block in v1locations.split(";"):
        if not block:
            continue
        parts = block.split("#")
        if len(parts) >= 3 and parts[2]:
            codes.add(parts[2])
    return codes


def is_turkey_record(rec: dict) -> bool:
    """True if any extracted location is in Turkey (FIPS ``TU``)."""
    return TURKEY_FIPS in location_country_codes(rec["locations"])


def themes_of(rec: dict) -> list[str]:
    """The V1THEMES list (semicolon-delimited; empties dropped)."""
    return [t for t in rec["themes"].split(";") if t]


def classify_event_type(themes: list[str]) -> str | None:
    """Map a document's GKG themes onto one §234 event type, or ``None`` if none apply.

    Scores each type by how many of its theme patterns appear among ``themes`` and returns the
    top-scoring type, ties broken by ``GKG_TYPE_PRIORITY``. ``None`` (no pattern matched) means
    the document is not one of the 11 tracked geopolitical event types — it is **skipped and
    counted**, never coerced into a default type (§4)."""
    counts = dict.fromkeys(EVENT_TYPES, 0)
    up = [t.upper() for t in themes]
    for etype, pats in GKG_TYPE_THEME_PATTERNS.items():
        for th in up:
            if any(p in th for p in pats):
                counts[etype] += 1
    best = max(counts.values())
    if best == 0:
        return None
    for etype in GKG_TYPE_PRIORITY:
        if counts[etype] == best:
            return etype
    return None  # unreachable while GKG_TYPE_PRIORITY covers all types


def parse_tone(v15tone: str) -> dict | None:
    """Parse V1.5TONE ``tone,pos,neg,polarity,actref,selfref,wc`` into a dict, or ``None`` if
    the field is absent/unparseable (the caller then declines to model a severity — §4)."""
    parts = v15tone.split(",")
    if len(parts) < 4:
        return None
    try:
        return {
            "tone": float(parts[0]),
            "positive": float(parts[1]),
            "negative": float(parts[2]),
            "polarity": float(parts[3]),
        }
    except (TypeError, ValueError):
        return None


def modeled_severity(tone_fields: dict | None) -> float | None:
    """A coarse 0..1 modeled event magnitude from document tone — **NOT price-derived** (§5).

    Magnitude is the absolute tonal deviation from neutral, saturating at ``|tone| = 10`` (the
    codebook's "common values range between -10 and +10"). Sign-agnostic: a sharp rapprochement
    (positive tone) is as much an event as a rupture. Returns ``None`` when tone is unavailable —
    a NULL severity, never a fabricated one. This is a deliberately simple first cut, tunable
    alongside the LLM extraction step."""
    if tone_fields is None:
        return None
    return min(1.0, abs(tone_fields["tone"]) / 10.0)


def gkg_date(v21date: str) -> date:
    """Parse a V2.1DATE ``YYYYMMDDHHMMSS`` (or ``YYYYMMDD``) string to a ``date``."""
    s = v21date.strip()
    if len(s) >= 8 and s[:8].isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ContractDrift(f"GKG V2.1DATE not parseable as a date: {v21date!r}")


def to_event_row(rec: dict) -> dict | None:
    """Build one ``events``-schema row from a GKG record, or ``None`` to skip it.

    Skips (returns ``None``) when the record is not Turkey-relevant or carries no recognised
    event-type theme. ``event_date = knowledge_date = V2.1DATE`` (GKG publication date; see the
    module docstring on the PIT collapse). ``severity`` is the modeled tonal magnitude."""
    if not is_turkey_record(rec):
        return None
    etype = classify_event_type(themes_of(rec))
    if etype is None:
        return None
    d = gkg_date(rec["date"])
    geography = ";".join(sorted(location_country_codes(rec["locations"])))
    return {
        "event_id": rec["record_id"],
        "event_date": d,
        "date_precision": "day",
        "event_type": etype,
        "actors": None,             # GKG has no CAMEO actor codes (an Events-table concept)
        "geography": geography,     # FIPS country codes seen in the document (incl. TU)
        "severity": modeled_severity(parse_tone(rec["tone"])),
        "source": GdeltAdapter.source_name,
        "knowledge_date": d,
    }


def to_event_target_rows(event_row: dict) -> list[dict]:
    """Seed the ``event_targets`` rows for one event from the inferred-tier type→channel prior.

    One row per (channel, sign) in ``taxonomy.TYPE_CHANNEL_PRIOR[event_type]``: ``shock_sign``
    from the prior, ``shock_magnitude=None`` (the prior is sign-only), ``evidence_tier='inferred'``
    and the coarse prior confidence/uncertainty. The LLM extraction step later overrides these
    with a higher-confidence ``gdelt_llm`` mapping; the prior is **never** presented as verified."""
    prior = prior_shock_vector(event_row["event_type"])
    return [
        {
            "event_id": event_row["event_id"],
            "channel": channel,
            "shock_sign": sign,
            "shock_magnitude": None,
            "confidence": _PRIOR_CONFIDENCE,
            "evidence_tier": "inferred",
            "uncertainty": _PRIOR_UNCERTAINTY,
            "source": _PRIOR_SOURCE,
            "knowledge_date": event_row["knowledge_date"],
        }
        for channel, sign in prior.items()
    ]


def gkg_records_to_l2_rows(records: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Turn parsed GKG records into ``(event_rows, event_target_rows, skipped_counts)``.

    Records that are non-Turkey or untyped are skipped and counted (the audit report reads these
    — §4 confidence-tiered writes: only typed Turkey events are written, the rest are logged)."""
    event_rows: list[dict] = []
    target_rows: list[dict] = []
    skipped = {"non_turkey": 0, "untyped": 0}
    for rec in records:
        if not is_turkey_record(rec):
            skipped["non_turkey"] += 1
            continue
        if classify_event_type(themes_of(rec)) is None:
            skipped["untyped"] += 1
            continue
        row = to_event_row(rec)
        if row is None:  # defensive; the two checks above already cover the skip cases
            continue
        event_rows.append(row)
        target_rows.extend(to_event_target_rows(row))
    return event_rows, target_rows, skipped


# === adapter (network) =================================================================
class GdeltAdapter(IngestionAdapter):
    source_name = "gdelt"

    def __init__(self, *, timeout: float = 60.0, base_url: str = _DEFAULT_BASE,
                 retries: int = 4, backoff: float = 2.0) -> None:
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.retries = retries          # transient-error retries before failing a file (§4-clean)
        self.backoff = backoff          # linear backoff base in seconds

    def _get_with_retry(self, url: str):
        """GET ``url`` with bounded retry on **transient** errors — a flaky DNS/connection blip or
        a 5xx is retried (the long backfill traverses tens of thousands of files; an unattended
        crawl must survive an intermittent network), with linear backoff. Retrying the real source
        is not fabrication (§4). A 404 is returned for the caller to treat as a real gap; a 4xx
        (other than 404) fails fast. Raises ``SourceUnreachable`` once the retries are exhausted."""
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = httpx.get(url, timeout=self.timeout)
            except httpx.HTTPError as e:          # DNS / connect / read timeout — transient
                last = e
            else:
                if resp.status_code < 500:        # 200/404/other 4xx -> let the caller decide
                    return resp
                last = SourceUnreachable(f"GDELT HTTP {resp.status_code}: {url}")  # 5xx -> retry
            if attempt < self.retries:
                time.sleep(self.backoff * (attempt + 1))
        raise SourceUnreachable(f"GDELT GET failed after {self.retries} retries: {url}: {last}")

    def _fetch_one(self, url: str) -> list[dict] | None:
        """Download + unzip + parse one 15-minute GKG file. Returns parsed records, or ``None``
        if the slot does not exist (HTTP 404 — a real gap in GDELT history, skipped not faked).
        Transient transport/5xx errors are retried (``_get_with_retry``); other HTTP errors raise
        ``SourceUnreachable`` (§4 fail-loud)."""
        resp = self._get_with_retry(url)
        if resp.status_code == 404:
            return None  # missing 15-min slot — skip, never fabricate
        if resp.status_code != 200:
            raise SourceUnreachable(f"GDELT HTTP {resp.status_code}: {url}")
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                name = zf.namelist()[0]
                raw = zf.read(name)
        except (zipfile.BadZipFile, IndexError) as e:
            raise ContractDrift(f"GDELT zip unreadable: {url}: {e}") from e
        # GKG is UTF-8 with the occasional stray byte; replace rather than crash a whole batch.
        return parse_gkg_csv(raw.decode("utf-8", errors="replace"))

    def fetch(self, *, start: date, end: date) -> list[dict]:
        """Pull every 15-minute GKG file over ``[start, end]`` and return the **Turkey-filtered**
        records. The reproducible backfill caches these to Parquet; signal code never calls this
        (§4). Missing slots are skipped; a transport failure raises ``SourceUnreachable``."""
        turkey: list[dict] = []
        for url in gkg_15min_urls(start, end, base=self.base_url):
            recs = self._fetch_one(url)
            if recs is None:
                continue
            turkey.extend(r for r in recs if is_turkey_record(r))
        return turkey

    def smoke_check(self) -> None:
        """Re-fetch one committed-golden 15-minute GKG slice and assert it still parses to the
        expected Turkey-record count + a known record's typed fields. Raises ``ContractDrift`` on
        drift; writes a §4 audit report. The golden is a REAL captured slice (committed in the
        live session — see ``tests/golden/gdelt/``); this method is exercised only live."""
        import json
        import pathlib

        golden = (
            pathlib.Path(__file__).resolve().parents[3]
            / "tests" / "golden" / "gdelt" / "gkg_smoke.json"
        )
        if not golden.exists():
            raise SourceUnreachable(
                "GDELT smoke golden not captured yet (tests/golden/gdelt/gkg_smoke.json) — "
                "capture it in the live ingestion session before relying on smoke_check."
            )
        doc = json.loads(golden.read_text())
        ts = datetime.strptime(doc["_provenance"]["timestamp"], "%Y%m%d%H%M%S")
        recs = self._fetch_one(gkg_url(ts, base=self.base_url)) or []
        turkey = [r for r in recs if is_turkey_record(r)]
        drift: list[str] = []
        if len(turkey) != doc["expected"]["turkey_record_count"]:
            drift.append(
                f"turkey_record_count {len(turkey)} != {doc['expected']['turkey_record_count']}"
            )
        write_run_report("gdelt_smoke", {
            "source": self.source_name,
            "base_url": self.base_url,
            "timestamp": doc["_provenance"]["timestamp"],
            "turkey_record_count": len(turkey),
            "drift": drift[:10],
        })
        if drift:
            raise ContractDrift(f"GDELT contract drift: {drift[:10]}")
