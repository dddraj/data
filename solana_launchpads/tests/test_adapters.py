"""End-to-end adapter tests over synthetic, byte-exact account fixtures."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LaunchpadDecoder, StaticAccountSource, pdas  # noqa: E402
from launchpad_decoder.anchor_idl import compile_idl  # noqa: E402
from launchpad_decoder.registry import WSOL, get as get_spec  # noqa: E402
from launchpad_decoder.types import CurveFamily, ValueSource  # noqa: E402
from tests.fixtures import encode_account  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
Q64 = 1 << 64
CURVE_ADDRESS = "So11111111111111111111111111111111111111112"  # any valid pubkey


def build(launchpad: str):
    spec = get_spec(launchpad)
    idl = json.loads((IDL_DIR / spec.idl_file).read_text())
    return spec, idl, compile_idl(idl, source=f"test:{spec.idl_file}")


def source_with(*accounts):
    source = StaticAccountSource(slot=1234)
    for pubkey, owner, data in accounts:
        source.add_raw(pubkey, owner, data)
    return source


def decoder_for(source):
    # `prefer_onchain_idl` stays on: the static source has no IDL account, so
    # this also exercises the bundled-schema fallback path.
    return LaunchpadDecoder(source, resolve_mints=False)


# --------------------------------------------------------------------------
# pump.fun
# --------------------------------------------------------------------------


def test_pumpfun_fresh_curve_reproduces_the_known_launch_numbers():
    spec, idl, schema = build("pumpfun")
    global_blob = encode_account(
        idl,
        schema,
        "Global",
        {
            "initialized": True,
            "initial_virtual_token_reserves": 1_073_000_000_000_000,
            "initial_virtual_sol_reserves": 30_000_000_000,
            "initial_real_token_reserves": 793_100_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
            "fee_basis_points": 100,
        },
    )
    curve_blob = encode_account(
        idl,
        schema,
        "BondingCurve",
        {
            "virtual_token_reserves": 1_073_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_token_reserves": 793_100_000_000_000,
            "real_quote_reserves": 0,
            "token_total_supply": 1_000_000_000_000_000,
            "complete": False,
        },
    )
    source = source_with(
        (pdas.pumpfun_global(), spec.program_id, global_blob),
        (CURVE_ADDRESS, spec.program_id, curve_blob),
    )
    m = decoder_for(source).decode_address(CURVE_ADDRESS)

    assert m is not None
    assert m.launchpad == "pumpfun"
    assert m.curve_account_type == "BondingCurve"
    assert m.curve_family is CurveFamily.CONSTANT_PRODUCT_VIRTUAL
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    assert m.launch_price_quote.value == pytest.approx(2.7958993e-8, rel=1e-6)
    assert m.launch_mcap_quote.value == pytest.approx(27.959, rel=1e-4)
    assert m.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)
    assert m.graduation_mcap_quote.value == pytest.approx(410.88, rel=1e-4)
    assert m.raised_quote.value == 0
    assert m.progress.value == 0
    assert m.complete.value is False
    assert m.fee_bps.value == 100
    # launch parameters came off chain, not from the bundled snapshot
    assert m.launch_price_quote.source is ValueSource.ONCHAIN_CONFIG
    assert m.current_price_quote.source is ValueSource.ONCHAIN_STATE


def test_pumpfun_mid_curve_progress_and_price():
    spec, idl, schema = build("pumpfun")
    global_blob = encode_account(
        idl,
        schema,
        "Global",
        {
            "initial_virtual_token_reserves": 1_073_000_000_000_000,
            "initial_virtual_sol_reserves": 30_000_000_000,
            "initial_real_token_reserves": 793_100_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
            "fee_basis_points": 100,
        },
    )
    # half the raise in: 42.5 SOL paid, reserves moved accordingly
    paid = 42_500_000_000
    v_quote = 30_000_000_000 + paid
    v_base = (1_073_000_000_000_000 * 30_000_000_000) // v_quote
    sold = 1_073_000_000_000_000 - v_base
    curve_blob = encode_account(
        idl,
        schema,
        "BondingCurve",
        {
            "virtual_token_reserves": v_base,
            "virtual_quote_reserves": v_quote,
            "real_token_reserves": 793_100_000_000_000 - sold,
            "real_quote_reserves": paid,
            "token_total_supply": 1_000_000_000_000_000,
            "complete": False,
        },
    )
    source = source_with(
        (pdas.pumpfun_global(), spec.program_id, global_blob),
        (CURVE_ADDRESS, spec.program_id, curve_blob),
    )
    m = decoder_for(source).decode_address(CURVE_ADDRESS)

    assert m.current_price_quote.value > m.launch_price_quote.value
    assert m.current_price_quote.value < m.graduation_price_quote.value
    assert m.progress.value == pytest.approx(0.5, rel=1e-3)
    assert m.tokens_sold.value == pytest.approx(sold / 1e6)


def test_pumpfun_without_a_node_falls_back_and_says_so():
    """No AccountSource -> bundled snapshot, clearly flagged."""
    spec, idl, schema = build("pumpfun")
    curve_blob = encode_account(
        idl,
        schema,
        "BondingCurve",
        {
            "virtual_token_reserves": 1_073_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_token_reserves": 793_100_000_000_000,
            "real_quote_reserves": 0,
            "token_total_supply": 1_000_000_000_000_000,
        },
    )
    m = LaunchpadDecoder(None).decode_account_data(spec.program_id, curve_blob)
    assert m is not None
    assert m.launch_price_quote.source is ValueSource.BUNDLED_SNAPSHOT
    assert any("bundled snapshot" in w for w in m.warnings)
    # the numbers still come out right, they are just not provably current
    assert m.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)


# --------------------------------------------------------------------------
# Raydium LaunchLab
# --------------------------------------------------------------------------


def launchlab_pool(**overrides):
    state = {
        "base_decimals": 6,
        "quote_decimals": 9,
        "status": 0,
        "supply": 1_000_000_000_000_000,
        "total_base_sell": 793_100_000_000_000,
        "virtual_base": 1_073_000_000_000_000,
        "virtual_quote": 30_000_000_000,
        "real_base": 0,
        "real_quote": 0,
        "total_quote_fund_raising": 85_000_000_000,
        "migrate_fee": 0,
        "global_config": "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
        "platform_config": "FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1",
        "base_mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
        "quote_mint": WSOL,
        "vesting_schedule": {"total_locked_amount": 0},
    }
    state.update(overrides)
    return state


def run_launchlab(curve_type: int, **overrides):
    spec, idl, schema = build("raydium_launchlab")
    pool = launchlab_pool(**overrides)
    global_blob = encode_account(
        idl,
        schema,
        "GlobalConfig",
        {"curve_type": curve_type, "index": 0, "trade_fee_rate": 10_000, "quote_mint": WSOL},
    )
    platform_blob = encode_account(
        idl,
        schema,
        "PlatformConfig",
        {"fee_rate": 10_000, "creator_fee_rate": 0, "name": list(b"LetsBonk.fun")},
    )
    pool_blob = encode_account(idl, schema, "PoolState", pool)
    source = source_with(
        (pool["global_config"], spec.program_id, global_blob),
        (pool["platform_config"], spec.program_id, platform_blob),
        (CURVE_ADDRESS, spec.program_id, pool_blob),
    )
    return decoder_for(source).decode_address(CURVE_ADDRESS)


def test_launchlab_constant_product_curve():
    m = run_launchlab(0)
    assert m.curve_type == "constant_product"
    assert m.curve_family is CurveFamily.CONSTANT_PRODUCT_VIRTUAL
    assert m.launch_price_quote.value == pytest.approx(2.7958993e-8, rel=1e-6)
    assert m.raise_target_quote.value == pytest.approx(85.0)
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    # (85 SOL - 0 fee) spread over the 206.9M tokens kept back for migration
    assert m.graduation_price_quote.value == pytest.approx(
        85_000_000_000 / (1_000_000_000_000_000 - 793_100_000_000_000) * 1e-3, rel=1e-9
    )
    assert m.raw_state["_platform_name"] == "LetsBonk.fun"
    # 1% trade + 1% platform fee, expressed in bps
    assert m.fee_bps.value == pytest.approx(200.0)


def test_launchlab_fixed_price_curve_is_flat():
    m = run_launchlab(1)
    assert m.curve_type == "fixed_price"
    assert m.curve_family is CurveFamily.FIXED_PRICE
    assert m.current_price_quote.value == pytest.approx(m.launch_price_quote.value)


def test_launchlab_linear_curve_starts_at_zero():
    # LaunchLab sizes a linear curve as a = 2 * raise * 2^64 / totalSell^2,
    # with `a` (the slope) stored in `virtual_base` as a u64.
    raise_target = 85_000_000_000
    total_sell = 2 * raise_target * 1_000_000_000_000_000 // (3 * raise_target)
    slope = 2 * raise_target * Q64 // (total_sell * total_sell)
    assert slope < 2**64

    m = run_launchlab(2, virtual_base=slope, total_base_sell=total_sell, real_base=0)
    assert m.curve_type == "linear"
    assert m.curve_family is CurveFamily.LINEAR
    assert m.launch_price_quote.value == 0.0

    done = run_launchlab(
        2, virtual_base=slope, total_base_sell=total_sell, real_base=total_sell
    )
    # A linear curve ends at twice its average price, and the average price is
    # the raise spread over the tokens sold. The 1% tolerance is not slack in
    # the decoder: `a` is a u64, and for a 6-decimal token with this raise it
    # comes out around 7, so LaunchLab itself quantises the slope by ~0.8%.
    average = raise_target / total_sell
    assert slope < 100  # the quantisation is this coarse
    assert done.current_price_quote.value == pytest.approx(2 * average * 1e-3, rel=1e-2)
    assert done.current_price_quote.value > m.current_price_quote.value


def test_launchlab_status_maps_to_complete_and_migrated():
    assert run_launchlab(0, status=0).migrated.value is False
    assert run_launchlab(0, status=1).complete.value is True
    assert run_launchlab(0, status=2).migrated.value is True


# --------------------------------------------------------------------------
# Meteora DBC
# --------------------------------------------------------------------------


def test_meteora_dbc_reads_the_whole_launch_from_its_pool_config():
    spec, idl, schema = build("meteora_dbc")
    config_address = "GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7"
    # base 6 decimals, quote 9: a UI price of 1e-8 SOL is a raw price of 1e-5
    sqrt_start = int(math.sqrt(1e-5) * Q64)
    sqrt_migration = int(math.sqrt(1e-3) * Q64)  # 100x
    config_blob = encode_account(
        idl,
        schema,
        "PoolConfig",
        {
            "quote_mint": WSOL,
            "token_decimal": 6,
            "migration_option": 1,
            "sqrt_start_price": sqrt_start,
            "migration_sqrt_price": sqrt_migration,
            "migration_quote_threshold": 85_000_000_000,
            "swap_base_amount": 800_000_000_000_000,
            "migration_base_threshold": 200_000_000_000_000,
            "pre_migration_token_supply": 1_000_000_000_000_000,
            "post_migration_token_supply": 1_000_000_000_000_000,
            "pool_fees": {"base_fee": {"cliff_fee_numerator": 20_000_000}},
        },
    )
    pool_blob = encode_account(
        idl,
        schema,
        "VirtualPool",
        {
            "config": config_address,
            "base_mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            "base_reserve": 800_000_000_000_000,
            "quote_reserve": 0,
            "sqrt_price": sqrt_start,
            "is_migrated": 0,
        },
    )
    source = source_with(
        (config_address, spec.program_id, config_blob),
        (CURVE_ADDRESS, spec.program_id, pool_blob),
    )
    m = decoder_for(source).decode_address(CURVE_ADDRESS)

    assert m.curve_family is CurveFamily.SQRT_PIECEWISE
    assert m.base_decimals == 6
    assert m.launch_price_quote.value == pytest.approx(1e-8, rel=1e-6)
    assert m.graduation_price_quote.value == pytest.approx(1e-6, rel=1e-6)
    assert m.current_price_quote.value == pytest.approx(1e-8, rel=1e-6)
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    assert m.raise_target_quote.value == pytest.approx(85.0)
    assert m.launch_mcap_quote.value == pytest.approx(10.0, rel=1e-6)
    assert m.graduation_mcap_quote.value == pytest.approx(1000.0, rel=1e-6)
    assert m.fee_bps.value == pytest.approx(200.0)  # 2e7 / 1e9 -> 2%
    assert m.complete.value is False
    assert m.raw_state["_migration_target"] == "damm_v2"


def test_meteora_dbc_without_its_config_is_honest_about_it():
    spec, idl, schema = build("meteora_dbc")
    pool_blob = encode_account(
        idl,
        schema,
        "VirtualPool",
        {"config": "GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7", "sqrt_price": Q64},
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, pool_blob))
    m = decoder_for(source).decode_address(CURVE_ADDRESS)
    assert m.launch_price_quote.value is None
    assert m.raise_target_quote.value is None
    assert any("PoolConfig unavailable" in w for w in m.warnings)


# --------------------------------------------------------------------------
# Moonit
# --------------------------------------------------------------------------


def test_moonit_constant_product_solves_its_raise_from_a_market_cap_threshold():
    spec, idl, schema = build("moonit")
    curve_blob = encode_account(
        idl,
        schema,
        "CurveAccount",
        {
            "total_supply": 1_000_000_000_000_000_000,
            "curve_amount": 1_000_000_000_000_000_000,
            "mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            "decimals": 9,
            "collateral_currency": "Sol",
            "curve_type": "ConstantProductV1",
            "marketcap_threshold": 345_000_000_000,
            "marketcap_currency": "Sol",
            "coef_b": 0,
        },
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, curve_blob))
    m = decoder_for(source).decode_address(CURVE_ADDRESS)

    assert m.curve_type == "ConstantProductV1"
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    assert m.launch_price_quote.value == pytest.approx(2.7958993e-8, rel=1e-6)
    assert m.launch_mcap_quote.value == pytest.approx(27.959, rel=1e-4)
    # the SDK's own table: a 345 SOL cap is hit after 799_820_983.2 tokens
    assert m.tokens_for_sale.value == pytest.approx(799_820_983.2, rel=1e-6)
    assert m.raise_target_quote.value == pytest.approx(87.83, rel=1e-3)
    assert m.graduation_price_quote.value * m.tokens_for_sale.value == pytest.approx(
        345.0, rel=1e-6
    )
    # constants are compiled into the program, so they are flagged as such
    assert m.launch_price_quote.source is ValueSource.BUNDLED_SNAPSHOT


def test_moonit_linear_curve():
    spec, idl, schema = build("moonit")
    curve_blob = encode_account(
        idl,
        schema,
        "CurveAccount",
        {
            "total_supply": 1_000_000_000_000_000_000,
            "curve_amount": 1_000_000_000_000_000_000,
            "mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            "decimals": 9,
            "collateral_currency": "Sol",
            "curve_type": "LinearV1",
            "marketcap_threshold": 345_000_000_000,
            "marketcap_currency": "Sol",
            "coef_b": 0,
        },
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, curve_blob))
    m = decoder_for(source).decode_address(CURVE_ADDRESS)
    assert m.curve_family is CurveFamily.LINEAR
    assert m.launch_price_quote.value == 0.0  # coef_b == 0
    assert m.tokens_for_sale.value == pytest.approx(550_000_000)
    # graduation price x tokens sold reproduces the configured 345 SOL cap
    assert m.graduation_price_quote.value * m.tokens_for_sale.value == pytest.approx(
        345.0, rel=1e-9
    )
    # a linear curve raises exactly half of the final "market cap"
    assert m.raise_target_quote.value == pytest.approx(172.5, rel=1e-9)


def test_moonit_unknown_curve_type_does_not_invent_prices():
    spec, idl, schema = build("moonit")
    curve_blob = encode_account(
        idl,
        schema,
        "CurveAccount",
        {
            "total_supply": 10**18,
            "curve_amount": 10**18,
            "decimals": 9,
            "curve_type": "FlatCurveV1",
            "marketcap_threshold": 345_000_000_000,
            "collateral_currency": "Sol",
            "marketcap_currency": "Sol",
        },
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, curve_blob))
    m = decoder_for(source).decode_address(CURVE_ADDRESS)
    assert m.current_price_quote.value is None
    assert any("collateral collected" in w for w in m.warnings)


# --------------------------------------------------------------------------
# Heaven / Vertigo / GoFundMeme
# --------------------------------------------------------------------------


def test_heaven_reads_prices_off_the_pool_and_cross_checks_reserves():
    spec, idl, schema = build("heaven")
    pool_blob = encode_account(
        idl,
        schema,
        "liquidityPoolState",
        {
            "base_token_mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            "base_token_mint_decimals": 6,
            "quote_token_mint": WSOL,
            "quote_token_mint_decimals": 9,
            "base_token_vault_balance": 900_000_000_000_000,
            "quote_token_vault_balance": 35_000_000_000,
            "initial_base_token_vault_balance": 1_000_000_000_000_000,
            "initial_quote_token_vault_balance": 30_000_000_000,
            "min_price": 3e-8,
            "curr_price": 3.888e-8,
            "max_price": 4e-8,
            "min_mc": 30.0,
            "curr_mc": 38.88,
            "max_mc": 40.0,
            "swap_fee_numerator": 100,
            "swap_fee_denominator": 10_000,
        },
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, pool_blob))
    m = decoder_for(source).decode_address(CURVE_ADDRESS)

    assert m.curve_family is CurveFamily.AMM_VIRTUAL_RESERVES
    assert m.launch_price_quote.value == pytest.approx(3e-8)
    assert m.launch_mcap_quote.value == pytest.approx(30.0)
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    assert m.tokens_sold.value == pytest.approx(100_000_000)
    assert m.raised_quote.value == pytest.approx(5.0)
    assert m.complete.value is False
    assert m.fee_bps.value == pytest.approx(100.0)
    assert not [w for w in m.warnings if "disagrees" in w]


def test_heaven_flags_a_cached_price_that_contradicts_the_reserves():
    spec, idl, schema = build("heaven")
    pool_blob = encode_account(
        idl,
        schema,
        "liquidityPoolState",
        {
            "base_token_mint_decimals": 6,
            "quote_token_mint_decimals": 9,
            "initial_base_token_vault_balance": 1_000_000_000_000_000,
            "initial_quote_token_vault_balance": 30_000_000_000,
            "min_price": 9e-7,  # nowhere near 3e-8
        },
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, pool_blob))
    m = decoder_for(source).decode_address(CURVE_ADDRESS)
    assert any("disagrees" in w for w in m.warnings)


def test_vertigo_shift_is_the_launch_market_cap():
    spec, idl, schema = build("vertigo")
    pool_blob = encode_account(
        idl,
        schema,
        "Pool",
        {
            "enabled": True,
            "mint_a": WSOL,
            "mint_b": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            "token_a_reserves": 0,
            "token_b_reserves": 1_000_000_000_000_000_000,
            "shift": 100_000_000_000,  # 100 SOL
            "royalties": 100,
        },
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, pool_blob))
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder._mint_decimals["MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"] = 9
    m = decoder.decode_address(CURVE_ADDRESS)

    assert m.launch_mcap_quote.value == pytest.approx(100.0)
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    assert m.launch_price_quote.value == pytest.approx(1e-7)
    assert m.raise_target_quote.value is None
    assert any("no graduation target" in w for w in m.warnings)


def test_gofundmeme_power_law_curve():
    spec, idl, schema = build("gofundmeme")
    pool_blob = encode_account(
        idl,
        schema,
        "BondingCurvePool",
        {
            "token_a_mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            "token_b_mint": WSOL,
            "total_supply": 1_000_000_000_000_000_000,
            "initial_tokens": 800_000_000_000_000_000,
            "token_balance": 800_000_000_000_000_000,
            "target_raise": 85_000_000_000,
            "current_sol": 0,
            "total_raised": 0,
            "curve_constant": 30.0,
            "curve_exponent": 1.0,
            "crank_reward_bps": 100,
        },
    )
    source = source_with((CURVE_ADDRESS, spec.program_id, pool_blob))
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder._mint_decimals["MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"] = 9
    m = decoder.decode_address(CURVE_ADDRESS)

    assert m.curve_family is CurveFamily.POWER_LAW
    assert m.raise_target_quote.value == pytest.approx(85.0)
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    assert 0 < m.launch_price_quote.value < m.graduation_price_quote.value
    assert m.current_price_quote.value == pytest.approx(m.launch_price_quote.value)
    # with exponent 1 the price ratio is exactly (target + c) / c
    assert m.graduation_price_quote.value / m.launch_price_quote.value == pytest.approx(
        (85.0 + 30.0) / 30.0, rel=1e-9
    )


# --------------------------------------------------------------------------
# cross-cutting
# --------------------------------------------------------------------------


def test_accounts_from_the_wrong_program_are_not_decoded():
    spec, idl, schema = build("pumpfun")
    blob = encode_account(idl, schema, "BondingCurve", {"virtual_token_reserves": 1})
    decoder = LaunchpadDecoder(None)
    assert decoder.decode_account_data("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj", blob) is None
    assert decoder.decode_account_data("11111111111111111111111111111111", blob) is None


def test_non_curve_accounts_of_a_known_program_are_skipped():
    spec, idl, schema = build("pumpfun")
    blob = encode_account(idl, schema, "Global", {"token_total_supply": 1})
    assert LaunchpadDecoder(None).decode_account_data(spec.program_id, blob) is None


def test_classify_names_the_account_without_pricing_it():
    spec, idl, schema = build("pumpfun")
    blob = encode_account(idl, schema, "Global", {})
    source = source_with((CURVE_ADDRESS, spec.program_id, blob))
    account = source.get_account(CURVE_ADDRESS)
    assert LaunchpadDecoder(source).classify(account) == ("pumpfun", "Global")


def test_metrics_serialise_with_provenance():
    spec, idl, schema = build("pumpfun")
    blob = encode_account(
        idl,
        schema,
        "BondingCurve",
        {
            "virtual_token_reserves": 1_073_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_token_reserves": 793_100_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
        },
    )
    m = LaunchpadDecoder(None).decode_account_data(spec.program_id, blob)
    payload = m.as_dict()
    assert payload["launchpad"] == "pumpfun"
    assert payload["launch_price_quote"]["source"] == "bundled_snapshot"
    assert payload["current_price_quote"]["source"] == "onchain_state"
    assert json.dumps(payload)  # must be JSON-serialisable
    usd = m.mcap_in(150.0)
    assert usd["launch_mcap_usd"] == pytest.approx(27.959 * 150.0, rel=1e-4)
