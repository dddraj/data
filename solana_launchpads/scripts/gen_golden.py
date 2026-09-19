#!/usr/bin/env python3
"""Generate cross-language golden vectors.

The Go package is a port, and a port is only worth anything if it agrees with
the reference to the last decimal. This writes account bytes plus the numbers
the Python decoder produces from them; the Go test suite reads the same file
and asserts it computes the same. Any divergence fails the Go build.

    python scripts/gen_golden.py && (cd go && go test ./...)

One vector per launchpad adapter that exists on both sides. Every case carries
the *bytes*, not the field values, so the two layout compilers are compared as
well as the two sets of maths.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import encode_account  # noqa: E402
from launchpad_decoder import LaunchpadDecoder, StaticAccountSource, pdas  # noqa: E402
from launchpad_decoder import curves  # noqa: E402
from launchpad_decoder.anchor_idl import compile_idl  # noqa: E402
from launchpad_decoder.registry import get as get_spec  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
OUT = ROOT / "go" / "launchpad" / "testdata" / "golden.json"

Q64 = 1 << 64
WSOL = "So11111111111111111111111111111111111111112"
CURVE_ADDRESS = "So11111111111111111111111111111111111111112"
BASE_MINT = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"

PUMP = get_spec("pumpfun")
IVT = 1_073_000_000_000_000
IVS = 30_000_000_000
IRT = 793_100_000_000_000
SUPPLY = 1_000_000_000_000_000

#: the invariant names the decoder can emit, so prose warnings are not
#: mistaken for violations
VIOLATION_NAMES = (
    "k_violation",
    "quote_below_open",
    "base_above_open",
    "nonstandard_opening",
    "quote_seed_not_positive",
    "base_floor_not_positive",
    "implied_opening_impossible",
    "implied_opening_exceeds_supply",
)


def load(idl_file: str):
    idl = json.loads((IDL_DIR / idl_file).read_text())
    return idl, compile_idl(idl)


def expect_from(m) -> Dict[str, Any]:
    """The numbers both languages must agree on, to ~15 significant figures."""
    return {
        "total_supply": m.total_supply.value,
        "tokens_for_sale": m.tokens_for_sale.value,
        "tokens_sold": m.tokens_sold.value,
        "launch_price": m.launch_price_quote.value,
        "current_price": m.current_price_quote.value,
        "graduation_price": m.graduation_price_quote.value,
        "launch_mcap": m.launch_mcap_quote.value,
        "current_mcap": m.current_mcap_quote.value,
        "graduation_mcap": m.graduation_mcap_quote.value,
        "raise_target": m.raise_target_quote.value,
        "raised": m.raised_quote.value,
        "progress": m.progress.value,
        "complete": bool(m.complete.value),
        "migrated": bool(m.migrated.value),
        "curve_type": m.curve_type,
        "fee_bps": m.fee_bps.value,
        "violations": sorted(
            name
            for name in VIOLATION_NAMES
            if any(w.startswith(name + ":") for w in m.warnings)
        ),
        "truncated": any("predates the current program layout" in w for w in m.warnings),
        "suspect_field": next(
            (
                w.split("suspect field: ")[1].split(" --")[0]
                for w in m.warnings
                if "suspect field:" in w
            ),
            "",
        ),
    }


# --------------------------------------------------------------------------
# pump.fun
# --------------------------------------------------------------------------

GLOBAL_FIELDS = {
    "initial_virtual_token_reserves": IVT,
    "initial_virtual_sol_reserves": IVS,
    "initial_real_token_reserves": IRT,
    "token_total_supply": SUPPLY,
    "fee_basis_points": 100,
}


def pumpfun_case(
    name: str,
    note: str,
    curve_fields: dict,
    truncate_to: Optional[int] = None,
    global_extra: Optional[dict] = None,
) -> dict:
    idl, schema = load("pump.json")

    config_blob = encode_account(idl, schema, "Global", {**GLOBAL_FIELDS, **(global_extra or {})})
    state_blob = encode_account(idl, schema, "BondingCurve", curve_fields)
    if truncate_to is not None:
        state_blob = state_blob[:truncate_to]

    source = StaticAccountSource(slot=1)
    source.add_raw(pdas.pumpfun_global(), PUMP.program_id, config_blob)
    source.add_raw(CURVE_ADDRESS, PUMP.program_id, state_blob)

    m = LaunchpadDecoder(source, resolve_mints=False).decode_address(CURVE_ADDRESS)
    assert m is not None, name
    return {
        "name": name,
        "note": note,
        "config_hex": config_blob.hex(),
        "state_hex": state_blob.hex(),
        "quote_decimals": 9,
        "expect": expect_from(m),
    }


def pumpfun_cases() -> list:
    paid = 42_500_000_000
    mid_quote = IVS + paid
    mid_base = (IVT * IVS) // mid_quote
    sold = IVT - mid_base
    variant_base = (IVT * 4_292_000_000) // 9_292_000_000

    return [
        pumpfun_case(
            "fresh_curve",
            "an untouched pump.fun curve: the canonical 28 SOL open, 85.005 SOL to graduate",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": IVS,
                "real_token_reserves": IRT,
                "real_quote_reserves": 0,
                "token_total_supply": SUPPLY,
            },
        ),
        pumpfun_case(
            "mid_curve",
            "half the raise paid in",
            {
                "virtual_token_reserves": mid_base,
                "virtual_quote_reserves": mid_quote,
                "real_token_reserves": IRT - sold,
                "real_quote_reserves": paid,
                "token_total_supply": SUPPLY,
            },
        ),
        pumpfun_case(
            "completed_curve",
            "curve drained, awaiting migration to PumpSwap",
            {
                "virtual_token_reserves": IVT - IRT,
                "virtual_quote_reserves": IVS + 85_005_359_057,
                "real_token_reserves": 0,
                "real_quote_reserves": 85_005_359_057,
                "token_total_supply": SUPPLY,
                "complete": True,
            },
        ),
        pumpfun_case(
            "field_mixing_anomaly",
            "the row a peer session measured in production. Given all four "
            "reserves this states an unfamiliar opening rather than a broken "
            "one -- k against a curve's own opening holds by construction -- so "
            "it is reported and still priced",
            {
                "virtual_token_reserves": 1_077_887_039_606_396,
                "virtual_quote_reserves": 18_204_928_211,
                "real_token_reserves": IRT,
                "real_quote_reserves": 1,
                "token_total_supply": SUPPLY,
            },
        ),
        pumpfun_case(
            "stale_base_against_a_known_opening",
            "the shape a second writer emitting a partial row really has: the "
            "quote side moved in lockstep so the curve classifies as the 30 SOL "
            "variant, but the base did not move with it. The opening is known, "
            "so k must hold, and it does not",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": IVS + 5_000_000_000,
                "real_token_reserves": IRT,
                "real_quote_reserves": 5_000_000_000,
                "token_total_supply": SUPPLY,
            },
        ),
        pumpfun_case(
            "virtual_column_holds_the_real_reserve",
            "the one signature that survives dropping the constants: no curve "
            "opens at zero, so a virtual quote equal to its real counterpart is "
            "impossible rather than merely unfamiliar",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": 670_000_000,
                "real_token_reserves": IRT,
                "real_quote_reserves": 670_000_000,
                "token_total_supply": SUPPLY,
            },
        ),
        pumpfun_case(
            "quote_variant_curve",
            "pump.fun seeds non-SOL-quoted curves at 4.292 rather than 30; judging "
            "one against the SOL opening makes every number wrong",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": 4_292_000_000,
                "real_token_reserves": IRT,
                "real_quote_reserves": 0,
                "token_total_supply": SUPPLY,
            },
            global_extra={"initial_virtual_quote_reserves": 4_292_000_000},
        ),
        pumpfun_case(
            "quote_variant_traded",
            "the same variant after trading, where a narrow band around the "
            "opening no longer finds it but the reserve offset still does",
            {
                "virtual_token_reserves": variant_base,
                "virtual_quote_reserves": 9_292_000_000,
                # the real base has to come down with the virtual one, or the
                # curve holds more tokens than it has
                "real_token_reserves": IRT - (IVT - variant_base),
                "real_quote_reserves": 5_000_000_000,
                "token_total_supply": SUPPLY,
            },
            global_extra={"initial_virtual_quote_reserves": 4_292_000_000},
        ),
        pumpfun_case(
            "legacy_truncated_account",
            "an account written before `creator` was appended; the prefix still decodes",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": IVS,
                "real_token_reserves": IRT,
                "real_quote_reserves": 0,
                "token_total_supply": SUPPLY,
            },
            truncate_to=8 + 8 * 5 + 1,
        ),
    ]


# --------------------------------------------------------------------------
# Raydium LaunchLab
# --------------------------------------------------------------------------

LAUNCHLAB = get_spec("raydium_launchlab")
GLOBAL_CONFIG_ADDRESS = "6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX"
PLATFORM_CONFIG_ADDRESS = "FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1"


def launchlab_case(name: str, note: str, curve_type: int, **pool_overrides) -> dict:
    idl, schema = load("raydium_launchpad.json")

    pool = {
        "base_decimals": 6,
        "quote_decimals": 9,
        "status": 0,
        "supply": SUPPLY,
        "total_base_sell": IRT,
        "virtual_base": IVT,
        "virtual_quote": IVS,
        "real_base": 0,
        "real_quote": 0,
        "total_quote_fund_raising": 85_000_000_000,
        "migrate_fee": 0,
        "global_config": GLOBAL_CONFIG_ADDRESS,
        "platform_config": PLATFORM_CONFIG_ADDRESS,
        "base_mint": BASE_MINT,
        "quote_mint": WSOL,
        "vesting_schedule": {"total_locked_amount": 0},
    }
    pool.update(pool_overrides)

    config_blob = encode_account(
        idl,
        schema,
        "GlobalConfig",
        {
            "curve_type": curve_type,
            "index": 0,
            "trade_fee_rate": 10_000,
            "migrate_fee": 0,
            "quote_mint": WSOL,
        },
    )
    platform_blob = encode_account(
        idl,
        schema,
        "PlatformConfig",
        {"fee_rate": 10_000, "creator_fee_rate": 0, "name": list(b"LetsBonk.fun")},
    )
    state_blob = encode_account(idl, schema, "PoolState", pool)

    source = StaticAccountSource(slot=1)
    source.add_raw(GLOBAL_CONFIG_ADDRESS, LAUNCHLAB.program_id, config_blob)
    source.add_raw(PLATFORM_CONFIG_ADDRESS, LAUNCHLAB.program_id, platform_blob)
    source.add_raw(CURVE_ADDRESS, LAUNCHLAB.program_id, state_blob)

    m = LaunchpadDecoder(source, resolve_mints=False).decode_address(CURVE_ADDRESS)
    assert m is not None, name
    return {
        "name": name,
        "note": note,
        "config_hex": config_blob.hex(),
        "platform_hex": platform_blob.hex(),
        "state_hex": state_blob.hex(),
        "expect": expect_from(m),
    }


def launchlab_cases() -> list:
    # A mid-curve pool, kept on its own k: LaunchLab writes the virtual pair
    # once and never moves it, so (vBase - realBase)(vQuote + realQuote) = k.
    paid = 42_500_000_000
    real_base = IVT - (IVT * IVS) // (IVS + paid)

    # LaunchLab sizes a linear curve as a = 2 * raise * 2^64 / totalSell^2,
    # with the slope stored in virtual_base as a u64.
    raise_target = 85_000_000_000
    total_sell = 2 * raise_target * SUPPLY // (3 * raise_target)
    slope = 2 * raise_target * Q64 // (total_sell * total_sell)

    return [
        launchlab_case(
            "constant_product_fresh",
            "a LetsBonk-style pool at creation, seeded with pump.fun's own reserves",
            0,
        ),
        launchlab_case(
            "constant_product_mid",
            "42.5 SOL paid in, reserves still on the k the virtual pair fixed",
            0,
            real_base=real_base,
            real_quote=paid,
        ),
        launchlab_case(
            "constant_product_migrated",
            "curve drained and migrated; graduation price is the raise less the "
            "migrate fee over the tokens held back",
            0,
            status=2,
            real_base=IRT,
            real_quote=raise_target,
            migrate_fee=1_000_000_000,
        ),
        launchlab_case(
            "fixed_price",
            "a flat curve: the price never moves off virtual_quote / virtual_base",
            1,
        ),
        launchlab_case(
            "linear_half_sold",
            "a linear curve halfway through its sellable supply; the slope lives "
            "in virtual_base as a Q64 value and quantises hard at u64",
            2,
            virtual_base=slope,
            total_base_sell=total_sell,
            real_base=total_sell // 2,
        ),
    ]


# --------------------------------------------------------------------------
# Meteora DBC
# --------------------------------------------------------------------------

DBC = get_spec("meteora_dbc")
DBC_CONFIG_ADDRESS = "GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7"


def dbc_case(
    name: str,
    note: str,
    config_fields: dict,
    pool_fields: dict,
    quote_decimals: int = 9,
) -> dict:
    idl, schema = load("meteora_dbc.json")

    config_blob = encode_account(idl, schema, "PoolConfig", config_fields)
    state_blob = encode_account(
        idl, schema, "VirtualPool", {"config": DBC_CONFIG_ADDRESS, **pool_fields}
    )

    source = StaticAccountSource(slot=1)
    source.add_raw(DBC_CONFIG_ADDRESS, DBC.program_id, config_blob)
    source.add_raw(CURVE_ADDRESS, DBC.program_id, state_blob)

    m = LaunchpadDecoder(source, resolve_mints=False).decode_address(CURVE_ADDRESS)
    assert m is not None, name
    return {
        "name": name,
        "note": note,
        "config_hex": config_blob.hex(),
        "state_hex": state_blob.hex(),
        "quote_decimals": quote_decimals,
        "expect": expect_from(m),
    }


def dbc_cases() -> list:
    # base 6 decimals, quote 9: a UI price of 1e-8 SOL is a raw price of 1e-5
    sqrt_start = int(math.sqrt(1e-5) * Q64)
    sqrt_migration = int(math.sqrt(1e-3) * Q64)  # 100x

    flat_config = {
        "quote_mint": WSOL,
        "token_decimal": 6,
        "migration_option": 1,
        "sqrt_start_price": sqrt_start,
        "migration_sqrt_price": sqrt_migration,
        "migration_quote_threshold": 85_000_000_000,
        "swap_base_amount": 800_000_000_000_000,
        "migration_base_threshold": 200_000_000_000_000,
        "pre_migration_token_supply": SUPPLY,
        "post_migration_token_supply": SUPPLY,
        "pool_fees": {"base_fee": {"cliff_fee_numerator": 20_000_000}},
    }

    # A two-segment curve with migration_sqrt_price left at zero, which is what
    # older configs do: both sides must walk the segments and land on the same
    # sqrt price, then on the same base amount sold.
    threshold = 85_000_000_000
    segments = [
        {"sqrt_price": int(math.sqrt(1e-4) * Q64), "liquidity": 2 * 10**32},
        {"sqrt_price": int(math.sqrt(1e-3) * Q64), "liquidity": 10**33},
    ]
    unpacked = curves.unpack_segments(segments)
    walked = curves.dbc_migration_sqrt_price(threshold, sqrt_start, unpacked)
    assert walked is not None, "segments cannot absorb the threshold"
    swap_base = curves.dbc_base_for_swap(sqrt_start, walked, unpacked)

    segment_config = {
        "quote_mint": WSOL,
        "token_decimal": 6,
        "migration_option": 0,
        "sqrt_start_price": sqrt_start,
        "migration_sqrt_price": 0,
        "migration_quote_threshold": threshold,
        "swap_base_amount": swap_base,
        "migration_base_threshold": 200_000_000_000_000,
        # dynamic supply: the total is summed from the curve's own parts
        "pre_migration_token_supply": 0,
        "locked_vesting_config": {
            "amount_per_period": 10_000_000_000_000,
            "number_of_period": 10,
            "cliff_unlock_amount": 5_000_000_000_000,
        },
        "curve": segments,
        "pool_fees": {"base_fee": {"cliff_fee_numerator": 25_000_000}},
    }

    return [
        dbc_case(
            "fresh_pool",
            "a Believe/Bags-style pool at creation: every static number comes "
            "off the shared PoolConfig, the pool carries only its sqrt price",
            flat_config,
            {
                "base_mint": BASE_MINT,
                "base_reserve": 800_000_000_000_000,
                "quote_reserve": 0,
                "sqrt_price": sqrt_start,
                "is_migrated": 0,
            },
        ),
        dbc_case(
            "mid_pool",
            "half the raise paid in and the price moved with it",
            flat_config,
            {
                "base_mint": BASE_MINT,
                "base_reserve": 400_000_000_000_000,
                "quote_reserve": 42_500_000_000,
                "sqrt_price": int(math.sqrt(1e-4) * Q64),
                "is_migrated": 0,
            },
        ),
        dbc_case(
            "migrated_pool",
            "threshold met and migrated to DAMM",
            flat_config,
            {
                "base_mint": BASE_MINT,
                "base_reserve": 0,
                "quote_reserve": 85_000_000_000,
                "sqrt_price": sqrt_migration,
                "is_migrated": 1,
            },
        ),
        dbc_case(
            "two_segment_derived_migration_price",
            "an older config with migration_sqrt_price at zero and a dynamic "
            "supply: both the migration price and the total supply have to be "
            "walked out of the curve's own segments",
            segment_config,
            {
                "base_mint": BASE_MINT,
                "base_reserve": swap_base,
                "quote_reserve": 0,
                "sqrt_price": sqrt_start,
                "is_migrated": 0,
            },
        ),
    ]


def main() -> None:
    sections = {
        "pumpfun": pumpfun_cases(),
        "raydium_launchlab": launchlab_cases(),
        "meteora_dbc": dbc_cases(),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(sections, indent=2) + "\n")

    total = sum(len(v) for v in sections.values())
    print(f"wrote {OUT.relative_to(ROOT)}: {total} vectors")
    for launchpad, cases in sections.items():
        print(f"  {launchpad}")
        for c in cases:
            e = c["expect"]
            launch = e["launch_price"] or 0.0
            mcap = e["current_mcap"] or 0.0
            print(
                f"    {c['name']:36} launch={launch:.6e} "
                f"mcap={mcap:>12.2f} target={e['raise_target']} "
                f"violations={e['violations']}"
            )


if __name__ == "__main__":
    main()
