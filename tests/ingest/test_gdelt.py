"""GDELT GKG adapter — offline unit guards + the live drift guard (M6).

The offline tests run in the fast inner loop (`make verify`): they pin the GKG 2.1 column
parse, the Turkey filter (FIPS `TU`), the theme→§234-type classifier + priority tie-break, the
modeled (non-price) severity, the PIT date collapse, the prior-seeded `event_targets`, and the
fail-loud / skip-and-count paths (§4/§5). They read a clearly-labelled ILLUSTRATIVE fixture
under `fixtures/gdelt/` — never real data, never L2.

The live tests hit the real GDELT raw feed and SKIP when it is unreachable or the smoke golden
has not been captured yet, so the offline suite still passes offline. Run explicitly:

    PYTHONPATH=src python -m pytest tests/ingest/test_gdelt.py -v
"""
from __future__ import annotations

import pathlib
from datetime import date, datetime

import httpx
import pytest

import tmkg.config as config
from tmkg.events.taxonomy import EVENT_TYPES, CHANNELS
from tmkg.ingest.gdelt import (
    GKG_TYPE_PRIORITY,
    GKG_TYPE_THEME_PATTERNS,
    GdeltAdapter,
    classify_event_type,
    gkg_15min_urls,
    gkg_date,
    gkg_records_to_l2_rows,
    gkg_url,
    is_turkey_record,
    location_country_codes,
    modeled_severity,
    parse_gkg_csv,
    parse_tone,
    to_event_row,
    to_event_target_rows,
)
from tmkg.pit.errors import ContractDrift, SourceUnreachable

FIXTURE = config.FIXTURES_PATH / "gdelt" / "gkg_sample.csv"


def _records() -> list[dict]:
    return parse_gkg_csv(FIXTURE.read_text())


# --- URL construction (deterministic, no master-list download) -------------------------
def test_gkg_url_is_15min_zip():
    u = gkg_url(datetime(2025, 3, 19, 3, 30, 0))
    assert u.endswith("/20250319033000.gkg.csv.zip")
    assert u.startswith("http://data.gdeltproject.org/gdeltv2/")


def test_15min_urls_cover_96_slots_per_day():
    urls = gkg_15min_urls(date(2025, 3, 19), date(2025, 3, 19))
    assert len(urls) == 96  # 24h * 4 slots
    assert urls[0].endswith("/20250319000000.gkg.csv.zip")
    assert urls[-1].endswith("/20250319234500.gkg.csv.zip")


def test_15min_urls_rejects_reversed_range():
    with pytest.raises(ValueError):
        gkg_15min_urls(date(2025, 3, 20), date(2025, 3, 19))


# --- parsing ---------------------------------------------------------------------------
def test_parse_drops_truncated_line_keeps_valid():
    recs = _records()
    # 5 well-formed rows; the deliberately 3-column trailing line is dropped.
    assert len(recs) == 5
    assert recs[0]["record_id"] == "20250319033000-1"


def test_parse_empty_body_is_empty_not_error():
    assert parse_gkg_csv("") == []
    assert parse_gkg_csv("\n  \n") == []


def test_parse_nonempty_but_unparseable_is_contract_drift():
    # A non-empty body whose rows never reach the 27-column layout = drift, not silent empty.
    with pytest.raises(ContractDrift):
        parse_gkg_csv("a\tb\tc\nd\te\tf\n")


def test_gkg_date_parses_timestamp_and_rejects_garbage():
    assert gkg_date("20250319033000") == date(2025, 3, 19)
    assert gkg_date("20250319") == date(2025, 3, 19)
    with pytest.raises(ContractDrift):
        gkg_date("not-a-date")


# --- Turkey filter (FIPS TU, not CAMEO TUR) --------------------------------------------
def test_location_country_codes_extracts_fips():
    codes = location_country_codes("4#Ankara, Ankara, Turkey#TU#TU81#39.93#32.86#-1")
    assert codes == {"TU"}


def test_turkey_filter_keeps_tu_drops_us():
    recs = _records()
    assert is_turkey_record(recs[0]) is True            # Ankara
    assert is_turkey_record(recs[2]) is False           # Washington (US-only)


# --- theme -> type classifier ----------------------------------------------------------
def test_classifier_maps_central_bank_to_cbrt():
    assert classify_event_type(["ECON_CENTRALBANK", "ECON_INTEREST_RATE", "EPU_POLICY"]) \
        == "cbrt_regulatory_action"


def test_classifier_maps_election():
    assert classify_event_type(["ELECTION", "DEMOCRACY", "TAX_FNCACT"]) \
        == "elections_political_transition"


def test_classifier_none_when_no_pattern_matches():
    assert classify_event_type(["TAX_FNCACT", "SOC_POINTSOFINTEREST"]) is None
    assert classify_event_type([]) is None


def test_classifier_priority_breaks_ties_toward_severe_type():
    # one terror theme vs one fx theme -> equal counts; priority puts terror_security first.
    assert classify_event_type(["TERROR", "ECON_CURRENCY"]) == "terror_security"


def test_classifier_priority_covers_all_types():
    # every event type must have patterns AND a place in the tie-break order (else a tie
    # could return None for a type that did match).
    assert set(GKG_TYPE_THEME_PATTERNS) == set(EVENT_TYPES)
    assert set(GKG_TYPE_PRIORITY) == set(EVENT_TYPES)


# --- severity (modeled, NOT price-derived) ---------------------------------------------
def test_parse_tone_and_severity_saturate_at_ten():
    tf = parse_tone("-12.0,0.5,12.5,13.0,28.0,0.9,450")
    assert tf["tone"] == pytest.approx(-12.0)
    assert modeled_severity(tf) == pytest.approx(1.0)          # |tone|>=10 saturates
    assert modeled_severity(parse_tone("-5.0,1,6,7,20,1,100")) == pytest.approx(0.5)


def test_severity_none_when_tone_unavailable():
    assert parse_tone("") is None
    assert modeled_severity(None) is None                      # NULL, never fabricated


# --- event row + PIT date collapse -----------------------------------------------------
def test_to_event_row_typed_turkey_record():
    row = to_event_row(_records()[0])
    assert row is not None
    assert row["event_id"] == "20250319033000-1"
    assert row["event_type"] == "cbrt_regulatory_action"
    assert row["date_precision"] == "day"
    assert row["source"] == "gdelt"
    assert row["geography"] == "TU"
    assert row["actors"] is None                               # GKG has no CAMEO actors
    # PIT: GKG publication date is both the event date AND the knowledge date (§5).
    assert row["event_date"] == date(2025, 3, 19)
    assert row["knowledge_date"] == row["event_date"]


def test_to_event_row_skips_non_turkey_and_untyped():
    recs = _records()
    assert to_event_row(recs[2]) is None                       # US record
    assert to_event_row(recs[3]) is None                       # Turkey but untyped


# --- event_targets seeded from the inferred-tier prior ---------------------------------
def test_event_targets_are_inferred_tier_sign_only():
    row = to_event_row(_records()[0])
    targets = to_event_target_rows(row)
    assert targets, "cbrt prior must seed at least one channel"
    for t in targets:
        assert t["event_id"] == "20250319033000-1"
        assert t["channel"] in CHANNELS
        assert t["shock_sign"] in (-1, 1)
        assert t["shock_magnitude"] is None                    # prior is sign-only
        assert t["evidence_tier"] == "inferred"                # never silently promoted (§5)
        assert t["source"] == "taxonomy_prior"
        assert t["knowledge_date"] == row["knowledge_date"]
    # cbrt prior is (rates_cds +1, fx -1) per taxonomy.TYPE_CHANNEL_PRIOR
    by_ch = {t["channel"]: t["shock_sign"] for t in targets}
    assert by_ch["rates_cds"] == 1 and by_ch["fx"] == -1


# --- the L2-row assembler: confidence-tiered, skips counted ----------------------------
def test_records_to_l2_rows_writes_typed_turkey_counts_skips():
    event_rows, target_rows, skipped = gkg_records_to_l2_rows(_records())
    ids = {r["event_id"] for r in event_rows}
    # rows 1 (cbrt), 2 (election), 5 (terror) are typed Turkey events; 3 non-TU, 4 untyped.
    assert ids == {"20250319033000-1", "20250319033000-2", "20250319033000-5"}
    assert skipped == {"non_turkey": 1, "untyped": 1}
    # every event contributed >=1 target row, all referencing a kept event
    assert {t["event_id"] for t in target_rows} == ids


# --- transient-error retry (the unattended backfill must survive a flaky network) -------
def test_get_with_retry_recovers_from_transient_then_succeeds(monkeypatch):
    a = GdeltAdapter(retries=3, backoff=0.0)
    calls = {"n": 0}

    def flaky_get(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:  # two transient DNS-style failures, then a 200
            raise httpx.ConnectError("nodename nor servname provided")
        return httpx.Response(200, content=b"ok", request=httpx.Request("GET", url))

    monkeypatch.setattr("tmkg.ingest.gdelt.httpx.get", flaky_get)
    monkeypatch.setattr("tmkg.ingest.gdelt.time.sleep", lambda s: None)
    resp = a._get_with_retry("http://x/y.zip")
    assert resp.status_code == 200 and calls["n"] == 3  # retried twice, then served


def test_get_with_retry_raises_after_exhausting(monkeypatch):
    a = GdeltAdapter(retries=2, backoff=0.0)

    def always_fail(url, **kw):
        raise httpx.ConnectError("dns down")

    monkeypatch.setattr("tmkg.ingest.gdelt.httpx.get", always_fail)
    monkeypatch.setattr("tmkg.ingest.gdelt.time.sleep", lambda s: None)
    with pytest.raises(SourceUnreachable):
        a._get_with_retry("http://x/y.zip")


def test_404_is_not_retried_returns_none(monkeypatch):
    a = GdeltAdapter(retries=3, backoff=0.0)
    calls = {"n": 0}

    def not_found(url, **kw):
        calls["n"] += 1
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr("tmkg.ingest.gdelt.httpx.get", not_found)
    assert a._fetch_one("http://x/y.zip") is None
    assert calls["n"] == 1  # a real gap (404) fails fast, never retried


# --- live drift guard (skips when GDELT/golden unavailable) -----------------------------
@pytest.mark.live
def test_live_smoke_check_or_skip():
    a = GdeltAdapter(timeout=30.0)
    try:
        a.smoke_check()
    except SourceUnreachable as e:
        pytest.skip(f"GDELT smoke unavailable (feed down or golden not captured): {e}")
