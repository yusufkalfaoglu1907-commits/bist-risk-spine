"""M6 event taxonomy — well-formedness + the channel vocabulary lock (BUILD_PLAN.md M6).

The taxonomy is the shared vocabulary the whole event engine routes through, so it is pinned
here before any signal consumes it: the 11 §234 categories, the channels = factor-ladder roles
identity, and the modeled type→channel prior staying honest to both.
"""
from __future__ import annotations

import pytest

from tmkg.events import taxonomy as tax
from tmkg.factors.neutralize import DEFAULT_LADDER


def test_validate_passes_on_the_shipped_taxonomy():
    tax.validate()  # raises on any malformation


def test_eleven_design_categories():
    # system-design-v2.md §234 lists 11 top-level event types.
    assert len(tax.EVENT_TYPES) == 11
    assert len(set(tax.EVENT_TYPES)) == 11


def test_channels_are_exactly_the_factor_ladder_roles():
    # the whole reuse claim: a channel IS a ladder role, so exposure = beta to that rung's factor.
    assert tax.CHANNELS == frozenset(DEFAULT_LADDER)


def test_prior_covers_every_type_and_only_known_channels():
    assert set(tax.TYPE_CHANNEL_PRIOR) == set(tax.EVENT_TYPES)
    for etype, pairs in tax.TYPE_CHANNEL_PRIOR.items():
        chans = [ch for ch, _ in pairs]
        assert chans, f"{etype} has an empty prior"
        assert len(chans) == len(set(chans)), f"{etype} repeats a channel"
        for ch, sign in pairs:
            assert ch in tax.CHANNELS
            assert sign in (-1, +1)


def test_prior_shock_vector_known_and_unknown():
    v = tax.prior_shock_vector("fx_monetary_shock")
    assert v["fx"] == +1 and v["rates_cds"] == +1 and v["market"] == -1
    with pytest.raises(KeyError):
        tax.prior_shock_vector("not_a_real_event_type")


def test_validate_catches_a_bad_sign(monkeypatch):
    broken = dict(tax.TYPE_CHANNEL_PRIOR)
    broken["pandemic"] = (("market", 0),)  # 0 is not a valid sign
    monkeypatch.setattr(tax, "TYPE_CHANNEL_PRIOR", broken)
    with pytest.raises(ValueError):
        tax.validate()


def test_validate_catches_an_unknown_channel(monkeypatch):
    broken = dict(tax.TYPE_CHANNEL_PRIOR)
    broken["pandemic"] = (("not_a_channel", -1),)
    monkeypatch.setattr(tax, "TYPE_CHANNEL_PRIOR", broken)
    with pytest.raises(ValueError):
        tax.validate()
