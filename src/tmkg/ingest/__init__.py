"""tmkg.ingest — ingestion adapters: the ONLY layer that may touch the network (§4).

Each external source = one adapter with a smoke_check() drift guard, landing
bitemporal rows in L2. Signal code never imports this package — it reads L2.
"""
from tmkg.ingest.base import IngestionAdapter
from tmkg.ingest.matriks import MatriksAdapter

__all__ = ["IngestionAdapter", "MatriksAdapter"]
