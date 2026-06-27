"""Substrate hardening monitors (M8.3) — make the standing health of the substrate observable.

Non-alpha, non-network checks over the local L1/L2 cache that surface decay before it corrupts a
result: the id-bridge single-point-of-failure (CLAUDE.md §5), data-source drift, registry hygiene.
Each emits a §4-style JSON audit report and is paired with a regression invariant that fails loudly
when health degrades — the v1 confidence-tiered / smoke-check pattern applied to the substrate itself.
"""
