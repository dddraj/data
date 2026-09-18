"""Finding and triaging curves that have no pool row.

These cover the bootstrap gap: a launchpad whose coins never produced a pool
row can never be learned from pool rows, so it has to be recognised from the
creator program alone.
"""

from __future__ import annotations

import json
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LaunchpadDecoder, StaticAccountSource  # noqa: E402
from launchpad_decoder.anchor_idl import account_discriminator, compile_idl  # noqa: E402
from launchpad_decoder.base58 import b58decode  # noqa: E402
from launchpad_decoder.discovery import (  # noqa: E402
    KNOWN,
    NOT_A_PROGRAM,
    NO_CURVE_SHAPE,
    OPAQUE,
    SELF_DESCRIBING,
    find_state_accounts_for_mint,
    mint_field_offsets,
    price_mint,
    summarise_triage,
    triage_programs,
)
from launchpad_decoder.program_state import (  # noqa: E402
    BPF_LOADER_UPGRADEABLE,
    PROGRAMDATA_HEADER_LEN,
    idl_address,
    programdata_address,
)
from launchpad_decoder.registry import get as get_spec  # noqa: E402
from tests.fixtures import encode_account  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
PUMP = get_spec("pumpfun")
LAUNCHLAB = get_spec("raydium_launchlab")
AUTHORITY = "2P56vRWDrCBGkqYXxgSWAnuZQZrJPySRQGToTJThpmkN"


def fake_pubkey(label: str) -> str:
    """A deterministic, valid pubkey that is certainly not in the registry."""
    import hashlib

    from launchpad_decoder.base58 import b58encode

    return b58encode(hashlib.sha256(label.encode()).digest())


MINT = fake_pubkey("mint-a")
OTHER_MINT = fake_pubkey("mint-b")
CURVE_A = fake_pubkey("curve-a")
CURVE_B = fake_pubkey("curve-b")
#: three creator programs that no adapter knows about
UNKNOWN_PROGRAM = fake_pubkey("mystery-launchpad")
REGISTRY_PROGRAM = fake_pubkey("not-a-launchpad")
OPAQUE_PROGRAM = fake_pubkey("no-idl-at-all")

CURVE_IDL = {
    "address": UNKNOWN_PROGRAM,
    "metadata": {"name": "mystery_pad", "version": "1.2.3"},
    "accounts": [{"name": "BondingCurve"}],
    "types": [
        {
            "name": "BondingCurve",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "virtual_token_reserves", "type": "u64"},
                    {"name": "virtual_sol_reserves", "type": "u64"},
                    {"name": "real_token_reserves", "type": "u64"},
                    {"name": "real_sol_reserves", "type": "u64"},
                    {"name": "token_total_supply", "type": "u64"},
                    {"name": "initial_real_token_reserves", "type": "u64"},
                    {"name": "mint", "type": "pubkey"},
                    {"name": "complete", "type": "bool"},
                ],
            },
        }
    ],
}

NOT_A_LAUNCHPAD_IDL = {
    "address": REGISTRY_PROGRAM,
    "metadata": {"name": "some_registry", "version": "0.1.0"},
    "accounts": [{"name": "Membership"}],
    "types": [
        {
            "name": "Membership",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "owner", "type": "pubkey"},
                    {"name": "joined_at", "type": "i64"},
                ],
            },
        }
    ],
}


def idl_blob(document: dict) -> bytes:
    payload = zlib.compress(json.dumps(document).encode())
    return (
        account_discriminator("IdlAccount")
        + b58decode(AUTHORITY)
        + len(payload).to_bytes(4, "little")
        + payload
    )


def deploy(source: StaticAccountSource, program_id: str, slot: int = 100) -> None:
    pd = programdata_address(program_id)
    source.add_raw(
        program_id,
        BPF_LOADER_UPGRADEABLE,
        (2).to_bytes(4, "little") + b58decode(program_id),
        executable=True,
    )
    header = (3).to_bytes(4, "little") + slot.to_bytes(8, "little") + b"\x01" + b58decode(AUTHORITY)
    assert len(header) == PROGRAMDATA_HEADER_LEN
    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, header + b"\x7fELF")


# --------------------------------------------------------------------------
# locating a curve with no pool row
# --------------------------------------------------------------------------


def test_mint_field_offsets_are_found_for_every_bundled_launchpad():
    """Every adapter'd launchpad exposes a mint at a fixed, memcmp-able offset."""
    decoder = LaunchpadDecoder(None)
    for spec in decoder.by_key.values():
        if spec.requires_onchain_idl:
            continue
        schema = decoder.bundled_schema(spec.program_id)
        offsets = mint_field_offsets(schema)
        assert offsets, spec.key
        assert all(offset >= 8 for _t, _f, offset in offsets), spec.key


def test_find_curve_by_memcmp_on_the_mint_field():
    """LaunchLab stores base_mint on the pool, so a server-side filter finds it."""
    idl = json.loads((IDL_DIR / "raydium_launchpad.json").read_text())
    schema = compile_idl(idl)
    source = StaticAccountSource(slot=5)

    def pool(mint: str) -> bytes:
        return encode_account(
            idl,
            schema,
            "PoolState",
            {
                "base_decimals": 6,
                "quote_decimals": 9,
                "supply": 1_000_000_000_000_000,
                "total_base_sell": 793_100_000_000_000,
                "virtual_base": 1_073_000_000_000_000,
                "virtual_quote": 30_000_000_000,
                "total_quote_fund_raising": 85_000_000_000,
                "base_mint": mint,
                "vesting_schedule": {"total_locked_amount": 0},
            },
        )

    source.add_raw(CURVE_A, LAUNCHLAB.program_id, pool(MINT))
    source.add_raw(CURVE_B, LAUNCHLAB.program_id, pool(OTHER_MINT))

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    found = find_state_accounts_for_mint(decoder, LAUNCHLAB.program_id, MINT)
    assert [c.address for c in found] == [CURVE_A]
    assert found[0].account_type == "PoolState"
    assert found[0].matched_field == "base_mint"
    # LaunchLab puts base_mint 205 bytes in -- the filter runs on the node
    assert found[0].memcmp_offset == 205


def test_find_curve_by_pda_when_the_mint_is_not_stored_on_the_curve():
    """pump.fun's BondingCurve carries no base mint; only the PDA finds it.

    A memcmp-only implementation silently returns nothing here, which is
    exactly the kind of gap that leaves coins unpriced.
    """
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    assert "base_mint" not in dict(schema.account("BondingCurve").layout.fields)

    from launchpad_decoder import pdas

    curve_address = pdas.pumpfun_bonding_curve(MINT)
    source = StaticAccountSource(slot=5)
    source.add_raw(
        curve_address,
        PUMP.program_id,
        encode_account(
            idl,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": 1_073_000_000_000_000,
                "virtual_quote_reserves": 30_000_000_000,
                "real_token_reserves": 793_100_000_000_000,
                "token_total_supply": 1_000_000_000_000_000,
            },
        ),
    )

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    found = find_state_accounts_for_mint(decoder, PUMP.program_id, MINT)
    assert [c.address for c in found] == [curve_address]
    assert found[0].matched_field == "(PDA of mint)"
    assert found[0].account_type == "BondingCurve"

    metrics = price_mint(decoder, PUMP.program_id, MINT)
    assert metrics.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)


def test_find_curve_returns_nothing_for_an_unrelated_mint():
    idl = json.loads((IDL_DIR / "raydium_launchpad.json").read_text())
    schema = compile_idl(idl)
    source = StaticAccountSource(slot=5)
    source.add_raw(
        CURVE_A,
        LAUNCHLAB.program_id,
        encode_account(idl, schema, "PoolState", {"base_mint": OTHER_MINT}),
    )
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    assert find_state_accounts_for_mint(decoder, LAUNCHLAB.program_id, MINT) == []


def test_price_mint_goes_straight_from_mint_to_metrics():
    """The backfill call: creator_program + mint -> a priced curve, no pool row."""
    source = StaticAccountSource(slot=9)
    deploy(source, UNKNOWN_PROGRAM)
    source.add_raw(idl_address(UNKNOWN_PROGRAM), UNKNOWN_PROGRAM, idl_blob(CURVE_IDL))

    schema = compile_idl(CURVE_IDL)
    source.add_raw(
        "HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn",
        UNKNOWN_PROGRAM,
        encode_account(
            CURVE_IDL,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": 1_073_000_000_000_000,
                "virtual_sol_reserves": 30_000_000_000,
                "real_token_reserves": 793_100_000_000_000,
                "token_total_supply": 1_000_000_000_000_000,
                "initial_real_token_reserves": 793_100_000_000_000,
                "mint": MINT,
            },
        ),
    )

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder._mint_decimals[MINT] = 6
    metrics = price_mint(decoder, UNKNOWN_PROGRAM, MINT)

    assert metrics is not None
    assert metrics.current_price_quote.value == pytest.approx(2.7958993e-8, rel=1e-6)
    assert metrics.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)
    assert metrics.total_supply.value == pytest.approx(1_000_000_000)


def test_price_mint_returns_none_rather_than_a_guess():
    source = StaticAccountSource(slot=9)
    deploy(source, UNKNOWN_PROGRAM)
    source.add_raw(idl_address(UNKNOWN_PROGRAM), UNKNOWN_PROGRAM, idl_blob(CURVE_IDL))
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    assert price_mint(decoder, UNKNOWN_PROGRAM, MINT) is None


# --------------------------------------------------------------------------
# triage
# --------------------------------------------------------------------------


def test_triage_separates_the_worth_deriving_from_the_tail():
    source = StaticAccountSource(slot=9)
    deploy(source, PUMP.program_id)
    deploy(source, UNKNOWN_PROGRAM)
    deploy(source, REGISTRY_PROGRAM)
    source.add_raw(idl_address(UNKNOWN_PROGRAM), UNKNOWN_PROGRAM, idl_blob(CURVE_IDL))
    source.add_raw(idl_address(REGISTRY_PROGRAM), REGISTRY_PROGRAM, idl_blob(NOT_A_LAUNCHPAD_IDL))
    opaque = OPAQUE_PROGRAM
    deploy(source, opaque)

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    results = triage_programs(
        decoder,
        [UNKNOWN_PROGRAM, REGISTRY_PROGRAM, opaque, PUMP.program_id, "not-a-pubkey"],
        coin_counts={UNKNOWN_PROGRAM: 21, REGISTRY_PROGRAM: 7, opaque: 4, PUMP.program_id: 1},
    )
    by_program = {r.program_id: r for r in results}

    # sorted by how many coins each unblocks
    assert [r.program_id for r in results][:3] == [UNKNOWN_PROGRAM, REGISTRY_PROGRAM, opaque]

    assert by_program[UNKNOWN_PROGRAM].verdict == SELF_DESCRIBING
    assert by_program[UNKNOWN_PROGRAM].idl_name == "mystery_pad"
    assert by_program[UNKNOWN_PROGRAM].curve_accounts == ["BondingCurve"]
    assert by_program[UNKNOWN_PROGRAM].decodable

    assert by_program[REGISTRY_PROGRAM].verdict == NO_CURVE_SHAPE
    assert not by_program[REGISTRY_PROGRAM].decodable

    assert by_program[opaque].verdict == OPAQUE
    assert "IDL" in by_program[opaque].note

    assert by_program[PUMP.program_id].verdict == KNOWN
    assert by_program[PUMP.program_id].launchpad_key == "pumpfun"

    assert by_program["not-a-pubkey"].verdict == NOT_A_PROGRAM


def test_triage_summary_answers_the_backlog_question():
    source = StaticAccountSource(slot=9)
    deploy(source, UNKNOWN_PROGRAM)
    source.add_raw(idl_address(UNKNOWN_PROGRAM), UNKNOWN_PROGRAM, idl_blob(CURVE_IDL))
    opaque = OPAQUE_PROGRAM
    deploy(source, opaque)

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    results = triage_programs(
        decoder,
        [UNKNOWN_PROGRAM, opaque],
        coin_counts={UNKNOWN_PROGRAM: 30, opaque: 3},
    )
    summary = summarise_triage(results)
    assert summary["coins_total"] == 33
    assert summary["programs_total"] == 2
    # one program unblocks 30 of the 33 -- a head, not a tail
    assert summary["coins_decodable"] == 30
    assert summary["programs_decodable"] == 1


def test_triage_flags_a_creator_program_that_is_not_executable():
    source = StaticAccountSource(slot=9)
    source.add_raw(MINT, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", b"\x00" * 82)
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    entry = triage_programs(decoder, [MINT])[0]
    assert entry.verdict == NOT_A_PROGRAM
    assert "not executable" in entry.note


def test_triage_without_a_node_still_recognises_registered_programs():
    decoder = LaunchpadDecoder(None)
    results = triage_programs(decoder, [PUMP.program_id, UNKNOWN_PROGRAM])
    by_program = {r.program_id: r for r in results}
    assert by_program[PUMP.program_id].verdict == KNOWN
    # without a node an unregistered program cannot be classified any further
    assert by_program[UNKNOWN_PROGRAM].verdict == OPAQUE
