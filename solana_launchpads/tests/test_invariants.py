"""Curve self-consistency.

Catches the failure mode where both reserves are individually plausible but
their product is not k -- which means they were not read from the same place.
Every schema-level check passes and the price is still wrong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LaunchpadDecoder, StaticAccountSource, pdas  # noqa: E402
from launchpad_decoder.anchor_idl import compile_idl  # noqa: E402
from launchpad_decoder.invariants import (  # noqa: E402
    check_constant_product_pair,
    check_metrics,
    check_reserve_offset,
    classify_variant,
    diagnose_pair,
)
from launchpad_decoder.registry import get as get_spec  # noqa: E402
from tests.fixtures import encode_account  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
PUMP = get_spec("pumpfun")

IVT = 1_073_000_000_000_000
IVS = 30_000_000_000
IRT = 793_100_000_000_000

#: The row a peer session measured in production: virtual base *above* its
#: opening and virtual quote *below* it, which a one-way curve cannot do.
#: vBase*vQuote is 61% of k, so the two fields did not come from one read.
FIELD_ANOMALY_BASE = 1_077_887_039_606_396
FIELD_ANOMALY_QUOTE = 18_204_928_211


def test_an_untouched_curve_is_consistent():
    assert check_constant_product_pair(IVT, IVS, IVT, IVS) == []


def test_a_normal_mid_curve_pair_is_consistent():
    paid = 42_500_000_000
    v_quote = IVS + paid
    v_base = (IVT * IVS) // v_quote
    assert check_constant_product_pair(v_base, v_quote, IVT, IVS) == []


def test_flooring_dust_does_not_trip_the_check():
    """Programs floor their outputs, so k creeps up. That must not be an error."""
    v_quote = IVS + 1_000_000_000
    v_base = (IVT * IVS) // v_quote + 5_000  # a few thousand base units of dust
    violations = check_constant_product_pair(v_base, v_quote, IVT, IVS)
    assert violations == []


def test_the_measured_production_anomaly_is_caught():
    violations = check_constant_product_pair(
        FIELD_ANOMALY_BASE, FIELD_ANOMALY_QUOTE, IVT, IVS
    )
    names = {v.name for v in violations}
    assert "k_violation" in names
    assert "quote_below_open" in names
    assert "base_above_open" in names
    detail = next(v.detail for v in violations if v.name == "k_violation")
    assert "-39" in detail  # k is 61% of the opening k


def test_diagnose_points_at_the_quote_field():
    suspect, explanation = diagnose_pair(
        FIELD_ANOMALY_BASE, FIELD_ANOMALY_QUOTE, IVT, IVS, IRT
    )
    assert suspect == "virtual_quote"
    # on the observed base, the quote should be ~29.864 SOL rather than 18.205
    assert "29.86" in explanation


def test_diagnose_points_at_the_base_field_when_that_is_the_odd_one():
    v_quote = IVS + 10_000_000_000  # legal: quote only ever rises
    v_base = IVT * 2  # illegal: base only ever falls
    suspect, _ = diagnose_pair(v_base, v_quote, IVT, IVS, IRT)
    assert suspect == "virtual_base"


def test_a_consistent_pair_has_no_suspect():
    suspect, explanation = diagnose_pair(IVT, IVS, IVT, IVS, IRT)
    assert suspect is None
    assert "consistent" in explanation


def test_missing_values_are_not_reported_as_violations():
    assert check_constant_product_pair(None, IVS, IVT, IVS) == []
    assert check_constant_product_pair(IVT, 0, IVT, IVS) == []
    assert diagnose_pair(None, None, IVT, IVS)[0] is None


# --------------------------------------------------------------------------
# through the decoder
# --------------------------------------------------------------------------


def decode_curve(virtual_base: int, virtual_quote: int, **extra):
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    source = StaticAccountSource(slot=5)
    source.add_raw(
        pdas.pumpfun_global(),
        PUMP.program_id,
        encode_account(
            idl,
            schema,
            "Global",
            {
                "initial_virtual_token_reserves": IVT,
                "initial_virtual_sol_reserves": IVS,
                "initial_real_token_reserves": IRT,
                "token_total_supply": 1_000_000_000_000_000,
                "fee_basis_points": 100,
            },
        ),
    )
    state = {
        "virtual_token_reserves": virtual_base,
        "virtual_quote_reserves": virtual_quote,
        "real_token_reserves": IRT,
        "token_total_supply": 1_000_000_000_000_000,
        **extra,
    }
    address = "So11111111111111111111111111111111111111112"
    source.add_raw(address, PUMP.program_id, encode_account(idl, schema, "BondingCurve", state))
    return LaunchpadDecoder(source, resolve_mints=False).decode_address(address)


def test_decoder_stays_silent_on_a_healthy_curve():
    m = decode_curve(IVT, IVS)
    assert not [w for w in m.warnings if "k_violation" in w]
    assert "SUSPECT" not in m.current_price_quote.note


def test_decoder_flags_the_production_anomaly_and_names_the_suspect_field():
    m = decode_curve(FIELD_ANOMALY_BASE, FIELD_ANOMALY_QUOTE)
    joined = " ".join(m.warnings)
    assert "k_violation" in joined
    assert "suspect field: virtual_quote" in joined
    assert "SUSPECT" in m.current_price_quote.note
    # the price is still reported -- flagged, not silently dropped
    assert m.current_price_quote.value == pytest.approx(1.688946e-08, rel=1e-4)


def test_the_flagged_price_is_the_one_that_would_have_been_wrong():
    """39% understated, which is the cost of not catching this."""
    bad = decode_curve(FIELD_ANOMALY_BASE, FIELD_ANOMALY_QUOTE)
    consistent_quote = (IVT * IVS) // FIELD_ANOMALY_BASE
    good = decode_curve(FIELD_ANOMALY_BASE, consistent_quote)
    assert not [w for w in good.warnings if "k_violation" in w]
    understatement = 1 - bad.current_price_quote.value / good.current_price_quote.value
    assert understatement == pytest.approx(0.39, abs=0.01)


def test_price_level_invariants_catch_a_sub_launch_price():
    m = decode_curve(IVT, IVS)
    m.current_price_quote.value = m.launch_price_quote.value / 2
    names = {v.name for v in check_metrics(m)}
    assert "price_below_launch" in names


def test_price_level_invariants_are_quiet_on_a_healthy_curve():
    paid = 42_500_000_000
    v_quote = IVS + paid
    v_base = (IVT * IVS) // v_quote
    m = decode_curve(v_base, v_quote, real_quote_reserves=paid)
    assert check_metrics(m) == []


# --------------------------------------------------------------------------
# variant classification, from the reserve offset
# --------------------------------------------------------------------------

IVQ = 4_292_000_000  # pump.fun's opening for non-SOL quote pairs
CANDIDATES = {"sol": IVS, "quote": IVQ}


def test_reserve_offset_classifies_a_traded_sol_curve():
    """The offset is fixed for life, so trading does not blur the classifier."""
    for paid in (0, 1_000_000_000, 42_500_000_000, 85_005_359_057):
        variant, initial = classify_variant(IVS + paid, paid, CANDIDATES)
        assert variant == "sol", paid
        assert initial == IVS


def test_reserve_offset_classifies_a_traded_quote_curve():
    """The 4.292 variant is the one a narrow band around the opening misses."""
    for paid in (0, 500_000_000, 12_000_000_000):
        variant, initial = classify_variant(IVQ + paid, paid, CANDIDATES)
        assert variant == "quote", paid
        assert initial == IVQ


def test_reserve_offset_reports_an_unclassifiable_pair():
    variant, offset = classify_variant(18_204_928_211, 1, CANDIDATES)
    assert variant is None
    assert offset == 18_204_928_210


def test_a_virtual_column_holding_the_real_reserve_is_named_as_such():
    """The residual signature: the two columns carry the same number."""
    violations = check_reserve_offset(670_000_000, 670_000_000, CANDIDATES)
    assert [v.name for v in violations] == ["reserve_offset_mismatch"]
    assert "carrying the REAL reserve" in violations[0].detail


def test_a_healthy_curve_raises_no_offset_violation():
    assert check_reserve_offset(IVS + 5_000_000_000, 5_000_000_000, CANDIDATES) == []
    assert check_reserve_offset(IVQ + 5_000_000_000, 5_000_000_000, CANDIDATES) == []


def test_missing_real_reserve_cannot_be_classified_but_is_not_a_violation():
    assert classify_variant(IVS, None, CANDIDATES) == (None, None)
    assert check_reserve_offset(IVS, None, CANDIDATES) == []


def decode_with_variants(virtual_quote, real_quote, virtual_base=None):
    """Decode a curve against a Global carrying BOTH opening constants."""
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    source = StaticAccountSource(slot=5)
    source.add_raw(
        pdas.pumpfun_global(),
        PUMP.program_id,
        encode_account(
            idl,
            schema,
            "Global",
            {
                "initial_virtual_token_reserves": IVT,
                "initial_virtual_sol_reserves": IVS,
                "initial_virtual_quote_reserves": IVQ,
                "initial_real_token_reserves": IRT,
                "token_total_supply": 1_000_000_000_000_000,
                "fee_basis_points": 100,
            },
        ),
    )
    address = "So11111111111111111111111111111111111111112"
    source.add_raw(
        address,
        PUMP.program_id,
        encode_account(
            idl,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": virtual_base if virtual_base else IVT,
                "virtual_quote_reserves": virtual_quote,
                "real_quote_reserves": real_quote,
                "real_token_reserves": IRT,
                "token_total_supply": 1_000_000_000_000_000,
            },
        ),
    )
    return LaunchpadDecoder(source, resolve_mints=False).decode_address(address)


def test_decoder_prices_a_quote_variant_curve_against_its_own_opening():
    """Previously this was judged against the 30 SOL opening and came out wrong."""
    m = decode_with_variants(IVQ, 0)
    assert m.curve_type == "constant_product:quote"
    assert not [w for w in m.warnings if "k_violation" in w]
    # 4.292e9 / 1.073e15 raw, and a 6-decimal quote leaves it unscaled
    assert m.launch_price_quote.value == pytest.approx(4.0e-6 * 1e-3, rel=1e-9)
    assert m.raise_target_quote.value == pytest.approx(12.162, rel=1e-3)


def test_decoder_still_prices_a_sol_variant_curve_the_same_way():
    m = decode_with_variants(IVS, 0)
    assert m.curve_type == "constant_product:sol"
    assert m.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)
    assert not m.warnings or not [w for w in m.warnings if "k_violation" in w]


def test_decoder_flags_the_residual_signature():
    """virtual_quote carrying the real reserve: offset collapses to zero."""
    m = decode_with_variants(670_000_000, 670_000_000)
    joined = " ".join(m.warnings)
    assert "reserve_offset_mismatch" in joined
    assert "carrying the REAL reserve" in joined
    assert m.curve_type == "constant_product:unclassified"
