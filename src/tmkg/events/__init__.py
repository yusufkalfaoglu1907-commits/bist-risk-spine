"""M6 — geopolitical event engine (BUILD_PLAN.md M6, system-design-v2.md §7.3).

Two outputs from one exposure model:
  * **alpha** — cross-sectional differential-exposure spread around events
    (``differential_exposure.py``);
  * **resilience** — channel-stress scenarios that re-price the book against a signed
    channel-shock vector (``channel_stress.py``).

Both route an event's incidence through the **channels** (factor-ladder roles) the
exposure tensor is already built on — so the event engine reuses the M2 ``betas`` and
the M3 residual substrate rather than inventing new machinery. The shared vocabulary
(event types, channels, the modeled type→channel prior) lives in ``taxonomy.py``.

Pure compute. The only network-touching piece is the GDELT ingestion adapter (§4), which
lives under ``tmkg.ingest``, not here.
"""
