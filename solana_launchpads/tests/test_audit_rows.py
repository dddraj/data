"""Column attribution: which on-chain field does a stored column actually hold?

The point of the audit is not to report a diff but to name the mapping bug, so
these tests check that a deliberately mis-mapped column is attributed to the
field whose value it really carries.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LaunchpadDecoder, StaticAccountSource, pdas  # noqa: E402
from launchpad_decoder.anchor_idl import compile_idl  # noqa: E402
from launchpad_decoder.registry import get as get_spec  # noqa: E402
from tests.fixtures import encode_account  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
PUMP = get_spec("pumpfun")

IVT = 1_073_000_000_000_000
IVS = 30_000_000_000
IRT = 793_100_000_000_000


def load_script():
    spec = importlib.util.spec_from_file_location("audit_rows", ROOT / "scripts" / "audit_rows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_rows = load_script()


def mint_for(label: str) -> str:
    import hashlib

    from launchpad_decoder.base58 import b58encode

    return b58encode(hashlib.sha256(label.encode()).digest())


def build(curves):
    """curves: list of (mint, virtual_quote, real_quote) -> a StaticAccountSource."""
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
    for mint, virtual_quote, real_quote in curves:
        v_base = (IVT * IVS) // virtual_quote
        # The real base has to come down with the virtual one, or the curve
        # holds more tokens than it has -- which the opening check now rejects.
        real_base = IRT - (IVT - v_base)
        source.add_raw(
            pdas.pumpfun_bonding_curve(mint),
            PUMP.program_id,
            encode_account(
                idl,
                schema,
                "BondingCurve",
                {
                    "virtual_token_reserves": v_base,
                    "virtual_quote_reserves": virtual_quote,
                    "real_token_reserves": real_base,
                    "real_quote_reserves": real_quote,
                    "token_total_supply": 1_000_000_000_000_000,
                },
            ),
        )
    return source


def test_correct_rows_attribute_to_their_own_field():
    mints = [mint_for(f"ok{i}") for i in range(4)]
    curves = [(m, IVS + 5_000_000_000 * (i + 1), 5_000_000_000 * (i + 1)) for i, m in enumerate(mints)]
    source = build(curves)
    decoder = LaunchpadDecoder(source, resolve_mints=False)

    rows = [
        {"mint": m, "virtual_quote_reserves": str(vq), "real_quote_reserves": str(rq)}
        for m, vq, rq in curves
    ]
    report = audit_rows.audit(decoder, rows, PUMP.program_id)

    assert report["rows_checked"] == 4
    assert report["attribution"]["virtual_quote_reserves"] == {"virtual_quote_reserves": 4}
    assert report["attribution"]["real_quote_reserves"] == {"real_quote_reserves": 4}
    assert report["rows_failing_curve_invariant_on_chain"] == 0


def test_a_swapped_column_is_attributed_to_the_field_it_really_holds():
    """The production symptom: the virtual column carrying the real reserve."""
    mints = [mint_for(f"swap{i}") for i in range(5)]
    curves = [(m, IVS + 7_000_000_000 * (i + 1), 7_000_000_000 * (i + 1)) for i, m in enumerate(mints)]
    source = build(curves)
    decoder = LaunchpadDecoder(source, resolve_mints=False)

    # two of five rows stored the REAL reserve in the VIRTUAL column
    rows = []
    for index, (m, vq, rq) in enumerate(curves):
        stored = rq if index < 2 else vq
        rows.append({"mint": m, "virtual_quote_reserves": str(stored)})

    report = audit_rows.audit(decoder, rows, PUMP.program_id)
    attribution = report["attribution"]["virtual_quote_reserves"]
    assert attribution["virtual_quote_reserves"] == 3
    assert attribution["real_quote_reserves"] == 2
    # the chain itself is fine -- the disagreement is entirely in the rows
    assert report["rows_failing_curve_invariant_on_chain"] == 0


def test_a_value_matching_nothing_on_chain_is_reported_with_an_example():
    mint = mint_for("orphan")
    source = build([(mint, IVS + 1_000_000_000, 1_000_000_000)])
    decoder = LaunchpadDecoder(source, resolve_mints=False)

    rows = [{"mint": mint, "virtual_quote_reserves": "18204928211"}]
    report = audit_rows.audit(decoder, rows, PUMP.program_id)

    assert report["attribution"]["virtual_quote_reserves"] == {
        "<no on-chain field holds this value>": 1
    }
    assert report["examples"][0]["stored_without_a_match"]["virtual_quote_reserves"] == 18204928211
    assert "virtual_quote_reserves" in report["examples"][0]["onchain"]


def test_identifier_columns_are_not_treated_as_values():
    mint = mint_for("ids")
    source = build([(mint, IVS, 0)])
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    rows = [{"mint": mint, "creator_program": PUMP.program_id, "virtual_quote_reserves": str(IVS)}]
    report = audit_rows.audit(decoder, rows, PUMP.program_id)
    assert set(report["attribution"]) == {"virtual_quote_reserves"}


def test_rows_for_missing_curves_are_counted_not_crashed_on():
    source = build([])
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    report = audit_rows.audit(decoder, [{"mint": mint_for("nope")}], PUMP.program_id)
    assert report["rows_unreadable"] == 1
    assert report["rows_checked"] == 0


def test_csv_values_are_parsed_leniently():
    assert audit_rows.as_int(" 1,073,000,000 ") == 1_073_000_000
    assert audit_rows.as_int("3.0e10") == 30_000_000_000
    assert audit_rows.as_int("NULL") is None
    assert audit_rows.as_int("") is None
    assert audit_rows.as_int(None) is None


def test_a_nonstandard_opening_is_not_reported_as_a_broken_row():
    """The distinction that matters, and the one this tool originally got wrong.

    A curve whose opening matches no constant in today's Global is not thereby
    broken. Measured against a real node, every stored column held exactly the
    on-chain field its name claimed and the curve still failed the old check --
    which was the check over-reaching, on roughly 9% of pump.fun curves.

    So an unfamiliar opening is reported, loudly, as its own category; only a
    structurally impossible one counts as a failure.
    """
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    source = build([])
    mint = mint_for("broken-chain")
    source.add_raw(
        pdas.pumpfun_bonding_curve(mint),
        PUMP.program_id,
        encode_account(
            idl,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": 1_077_887_039_606_396,
                "virtual_quote_reserves": 18_204_928_211,
                "real_token_reserves": IRT,
                "token_total_supply": 1_000_000_000_000_000,
            },
        ),
    )
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    report = audit_rows.audit(decoder, [{"mint": mint}], PUMP.program_id)
    assert report["rows_failing_curve_invariant_on_chain"] == 0
    assert report["rows_with_a_nonstandard_opening"] == 1
    # And the bucket an operator needs: the opening this curve actually claims.
    assert report["opening_buckets"] == {18_204_928_211: 1}


def test_structurally_impossible_state_is_still_a_failure():
    """A virtual reserve below its real counterpart is impossible on any curve."""
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    source = build([])
    mint = mint_for("impossible-chain")
    source.add_raw(
        pdas.pumpfun_bonding_curve(mint),
        PUMP.program_id,
        encode_account(
            idl,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": 670_000_000,
                "real_token_reserves": IRT,
                "real_quote_reserves": 670_000_000,
                "token_total_supply": 1_000_000_000_000_000,
            },
        ),
    )
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    report = audit_rows.audit(decoder, [{"mint": mint}], PUMP.program_id)
    assert report["rows_failing_curve_invariant_on_chain"] == 1
