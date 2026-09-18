"""Decoder-level features: platform discovery, scanning, registry integrity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LAUNCHPADS, LaunchpadDecoder, StaticAccountSource  # noqa: E402
from launchpad_decoder.adapters import ADAPTERS, get_adapter  # noqa: E402
from launchpad_decoder.anchor_idl import compile_idl  # noqa: E402
from launchpad_decoder.registry import (  # noqa: E402
    VERIFIED_PLATFORMS,
    BY_KEY,
    get as get_spec,
)
from tests.fixtures import encode_account  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
DBC = get_spec("meteora_dbc")
LAUNCHLAB = get_spec("raydium_launchlab")


def test_registry_is_internally_consistent():
    keys = [spec.key for spec in LAUNCHPADS]
    assert len(keys) == len(set(keys))
    programs = [spec.program_id for spec in LAUNCHPADS]
    assert len(programs) == len(set(programs))
    for spec in LAUNCHPADS:
        assert spec.adapter in ADAPTERS, spec.key
        if not spec.requires_onchain_idl:
            assert spec.idl_file, spec.key
            assert (IDL_DIR / spec.idl_file).exists(), spec.key


def test_unknown_launchpad_raises_with_a_useful_message():
    with pytest.raises(KeyError) as excinfo:
        get_spec("definitely-not-a-launchpad")
    assert "known:" in str(excinfo.value)


def test_lookup_works_by_key_and_by_program_id():
    assert get_spec("pumpfun") is get_spec("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")


def test_unknown_adapter_name_falls_back_to_the_heuristic_one():
    assert get_adapter("no-such-adapter") is ADAPTERS["heuristic"]


def test_verified_platforms_point_at_a_registered_program():
    for platform in VERIFIED_PLATFORMS:
        assert platform.launchpad_key in BY_KEY
        assert BY_KEY[platform.launchpad_key].program_id == platform.program_id
        assert platform.as_dict()["name"]


def test_discover_platforms_lists_dbc_configs():
    idl = json.loads((IDL_DIR / "meteora_dbc.json").read_text())
    schema = compile_idl(idl)
    source = StaticAccountSource(slot=3)
    for address, threshold in (
        ("GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7", 85_000_000_000),
        ("HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn", 500_000_000_000),
    ):
        source.add_raw(
            address,
            DBC.program_id,
            encode_account(
                idl,
                schema,
                "PoolConfig",
                {"migration_quote_threshold": threshold, "token_decimal": 6},
            ),
        )

    platforms = LaunchpadDecoder(source).discover_platforms("meteora_dbc")
    assert len(platforms) == 2
    thresholds = sorted(p.fields["migration_quote_threshold"] for p in platforms)
    assert thresholds == [85_000_000_000, 500_000_000_000]
    assert all(p.config_account_type == "PoolConfig" for p in platforms)


def test_discover_platforms_reads_launchlab_platform_names():
    idl = json.loads((IDL_DIR / "raydium_launchpad.json").read_text())
    schema = compile_idl(idl)
    source = StaticAccountSource(slot=3)
    source.add_raw(
        "FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1",
        LAUNCHLAB.program_id,
        encode_account(
            idl,
            schema,
            "PlatformConfig",
            {"name": list(b"LetsBonk.fun"), "fee_rate": 10_000},
        ),
    )
    platforms = LaunchpadDecoder(source).discover_platforms("raydium_launchlab")
    assert [p.name for p in platforms] == ["LetsBonk.fun"]
    assert platforms[0].fields["fee_rate"] == 10_000


def test_scan_program_decodes_every_curve_a_program_owns():
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    spec = get_spec("pumpfun")
    source = StaticAccountSource(slot=7)
    for address in (
        "GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7",
        "HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn",
    ):
        source.add_raw(
            address,
            spec.program_id,
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
    # a non-curve account of the same program must be skipped
    source.add_raw(
        "So11111111111111111111111111111111111111112",
        spec.program_id,
        encode_account(idl, schema, "Global", {}),
    )

    found = LaunchpadDecoder(source, resolve_mints=False).scan_program("pumpfun")
    assert len(found) == 2
    assert all(m.curve_account_type == "BondingCurve" for m in found)


def test_commands_needing_a_node_say_so_instead_of_crashing():
    decoder = LaunchpadDecoder(None)
    for call in (
        lambda: decoder.decode_address("So11111111111111111111111111111111111111112"),
        lambda: decoder.scan_program("pumpfun"),
        lambda: decoder.discover_platforms("meteora_dbc"),
    ):
        with pytest.raises(RuntimeError, match="AccountSource"):
            call()
    assert decoder.refresh().describe() == "no program or parameter changes"
    assert decoder.onchain_idl_status() == {}


def test_static_params_without_a_node_still_describes_the_launchpad():
    params = LaunchpadDecoder(None).static_params("meteora_dbc")
    assert params["program_id"] == DBC.program_id
    assert params["curve_family"] == "sqrt_piecewise"
    assert params["schema_source"].startswith("bundled:")
    assert params["configs"] == {}


def test_decoder_adopts_an_unregistered_program_that_publishes_an_idl():
    """The registry is a head start, not a gate."""
    import zlib

    from launchpad_decoder.anchor_idl import account_discriminator
    from launchpad_decoder.base58 import b58decode, b58encode
    from launchpad_decoder.program_state import idl_address

    program = b58encode(__import__("hashlib").sha256(b"brand-new-pad").digest())
    idl = {
        "address": program,
        "metadata": {"name": "brand_new_pad", "version": "0.1.0"},
        "accounts": [{"name": "Curve"}],
        "types": [
            {
                "name": "Curve",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "virtual_token_reserves", "type": "u64"},
                        {"name": "virtual_sol_reserves", "type": "u64"},
                        {"name": "token_total_supply", "type": "u64"},
                        {"name": "base_mint", "type": "pubkey"},
                    ],
                },
            }
        ],
    }
    payload = zlib.compress(json.dumps(idl).encode())
    source = StaticAccountSource(slot=1)
    source.add_raw(
        idl_address(program),
        program,
        account_discriminator("IdlAccount")
        + b58decode("2P56vRWDrCBGkqYXxgSWAnuZQZrJPySRQGToTJThpmkN")
        + len(payload).to_bytes(4, "little")
        + payload,
    )
    schema = compile_idl(idl)
    source.add_raw(
        "So11111111111111111111111111111111111111112",
        program,
        encode_account(
            idl,
            schema,
            "Curve",
            {
                "virtual_token_reserves": 1_073_000_000_000_000,
                "virtual_sol_reserves": 30_000_000_000,
                "token_total_supply": 1_000_000_000_000_000,
                "base_mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            },
        ),
    )

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder._mint_decimals["MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"] = 6
    metrics = decoder.decode_address("So11111111111111111111111111111111111111112")

    assert metrics is not None
    assert metrics.launchpad == "learned:brand_new_pad"
    assert program in decoder.learned_programs
    assert metrics.current_price_quote.value == pytest.approx(2.7958993e-8, rel=1e-6)
    assert metrics.total_supply.value == pytest.approx(1_000_000_000)


def test_auto_learn_can_be_turned_off():
    program = "GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7"  # registered
    decoder = LaunchpadDecoder(StaticAccountSource(), auto_learn=False)
    assert decoder._spec_for(program) is not None  # registry still works
    unknown = "So11111111111111111111111111111111111111112"
    assert decoder._spec_for(unknown) is None
    assert decoder.learned_programs == {}


def test_a_program_with_no_idl_is_recorded_as_unlearnable():
    unknown = "So11111111111111111111111111111111111111112"
    decoder = LaunchpadDecoder(StaticAccountSource())
    assert decoder.learn_program(unknown) is None
    assert "no IDL account" in decoder.unlearnable[unknown]
    # and it is not retried on every subsequent account
    assert decoder.learn_program(unknown) is None


def test_snapshot_static_params_never_raises_on_one_bad_launchpad():
    snapshot = LaunchpadDecoder(None).snapshot_static_params()
    assert set(snapshot) == {spec.key for spec in LAUNCHPADS}
    assert all("error" not in entry for entry in snapshot.values())
