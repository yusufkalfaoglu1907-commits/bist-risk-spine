# GDELT fixtures — ILLUSTRATIVE, NOT REAL DATA

`gkg_sample.csv` is a **hand-built, synthetic** GKG 2.1 slice used only by the offline
parser/classifier unit tests (`tests/ingest/test_gdelt.py`). The values (record IDs, tone,
themes, locations) are invented to exercise the Turkey filter, the theme→type classifier, the
severity model, and the malformed-line drop path.

Per CLAUDE.md §4 this is a labelled fixture under `fixtures/` — it must **never** reach L2.
The real point-in-time golden for the live `smoke_check` drift guard is a genuinely captured
15-minute GKG slice committed under `tests/golden/gdelt/` during the live ingestion session;
it is **not** in this directory and is **not** fabricated here.
