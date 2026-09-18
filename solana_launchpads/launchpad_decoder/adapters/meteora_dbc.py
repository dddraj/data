"""Meteora Dynamic Bonding Curve (Believe, Jupiter Studio, Bags, ...).

Program: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN

DBC is the general case.  A partner creates a `PoolConfig` describing the curve
as up to 20 concentrated-liquidity segments -- `(sqrt_price, liquidity)` pairs
in Q64.64 -- anchored at `sqrt_start_price`, plus a `migration_quote_threshold`
and the pre/post migration token supply.  Every `VirtualPool` then just tracks
`sqrt_price`, `base_reserve` and `quote_reserve`.

So the "static" launch numbers for a whole launchpad are one account read:
    launch price      = (sqrt_start_price / 2^64)^2
    graduation price  = (migration_sqrt_price / 2^64)^2
    supply            = pre_migration_token_supply
    raise target      = migration_quote_threshold
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext

MIGRATION_OPTIONS = {0: "damm_v1", 1: "damm_v2"}
COLLECT_FEE_MODES = {0: "quote_only", 1: "quote_and_base"}


class MeteoraDbcAdapter(Adapter):
    key = "meteora_dbc"
    curve_family = CurveFamily.SQRT_PIECEWISE

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        m.base_mint = state.get("base_mint")
        m.creator = state.get("creator")

        config_address = state.get("config")
        config: Dict[str, Any] = {}
        cfg_slot = 0
        if config_address:
            _name, decoded, cfg_slot = ctx.configs.fetch(
                ctx.spec.program_id, config_address, ctx.schema
            )
            config = decoded or {}
        if not config:
            m.warn(
                "PoolConfig unavailable: launch price, supply and raise target "
                "cannot be resolved without it"
            )

        m.quote_mint = config.get("quote_mint") or ctx.spec.default_quote_mint
        m.base_decimals = config.get("token_decimal")
        if m.base_decimals is None:
            m.base_decimals = ctx.decimals_for(m.base_mint)
        m.quote_decimals = ctx.decimals_for(m.quote_mint)
        m.curve_type = f"piecewise_{len(curves.active_segments(curves.unpack_segments(config.get('curve') or [])))}_segment"

        sqrt_start = config.get("sqrt_start_price")
        sqrt_migration = config.get("migration_sqrt_price")
        sqrt_now = state.get("sqrt_price")
        segments = curves.active_segments(curves.unpack_segments(config.get("curve") or []))
        threshold = config.get("migration_quote_threshold")

        if (not sqrt_migration) and sqrt_start and segments and threshold:
            sqrt_migration = curves.dbc_migration_sqrt_price(threshold, sqrt_start, segments)

        m.launch_price_quote = self.param(
            price_raw_to_ui(
                curves.sqrt_price_to_raw_price(sqrt_start) if sqrt_start else None,
                m.base_decimals,
                m.quote_decimals,
            ),
            ValueSource.ONCHAIN_CONFIG,
            cfg_slot,
            "(sqrt_start_price / 2^64)^2",
        )
        m.current_price_quote = self.param(
            price_raw_to_ui(
                curves.sqrt_price_to_raw_price(sqrt_now) if sqrt_now else None,
                m.base_decimals,
                m.quote_decimals,
            ),
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
            "(sqrt_price / 2^64)^2",
        )
        m.graduation_price_quote = self.param(
            price_raw_to_ui(
                curves.sqrt_price_to_raw_price(sqrt_migration) if sqrt_migration else None,
                m.base_decimals,
                m.quote_decimals,
            ),
            ValueSource.ONCHAIN_CONFIG if config.get("migration_sqrt_price") else ValueSource.DERIVED,
            cfg_slot,
            "(migration_sqrt_price / 2^64)^2",
        )

        # -- supply -------------------------------------------------------
        supply_raw = config.get("pre_migration_token_supply")
        supply_source = ValueSource.ONCHAIN_CONFIG
        if not supply_raw:
            # Dynamic-supply configs mint exactly what the curve sells plus
            # what migration needs.
            swap_base = config.get("swap_base_amount") or 0
            migration_base = config.get("migration_base_threshold") or 0
            locked = (config.get("locked_vesting_config") or {}).get("amount_per_period", 0) or 0
            periods = (config.get("locked_vesting_config") or {}).get("number_of_period", 0) or 0
            cliff = (config.get("locked_vesting_config") or {}).get("cliff_unlock_amount", 0) or 0
            supply_raw = swap_base + migration_base + locked * periods + cliff
            supply_source = ValueSource.DERIVED
        m.total_supply = self.param(
            ui_amount(supply_raw or None, m.base_decimals), supply_source, cfg_slot,
            "pre_migration_token_supply" if supply_source is ValueSource.ONCHAIN_CONFIG
            else "swap_base_amount + migration_base_threshold + locked vesting",
        )
        m.tokens_for_sale = self.param(
            ui_amount(config.get("swap_base_amount"), m.base_decimals),
            ValueSource.ONCHAIN_CONFIG,
            cfg_slot,
            "swap_base_amount",
        )

        # The curve's own view of how much base it will sell, recomputed from
        # the segments -- a useful cross-check on swap_base_amount.
        if sqrt_start and sqrt_migration and segments:
            derived_sale = curves.dbc_base_for_swap(sqrt_start, sqrt_migration, segments)
            configured = config.get("swap_base_amount")
            if configured and abs(derived_sale - configured) > max(1, configured // 1000):
                m.warn(
                    f"swap_base_amount ({configured}) disagrees with the curve "
                    f"segments ({derived_sale}) by more than 0.1%"
                )

        base_reserve = state.get("base_reserve")
        if base_reserve is not None and config.get("swap_base_amount"):
            m.tokens_sold = self.param(
                ui_amount(max(0, config["swap_base_amount"] - base_reserve), m.base_decimals),
                ValueSource.DERIVED,
                ctx.slot,
                "swap_base_amount - base_reserve",
            )

        # -- the raise ----------------------------------------------------
        m.raise_target_quote = self.param(
            ui_amount(threshold, m.quote_decimals),
            ValueSource.ONCHAIN_CONFIG,
            cfg_slot,
            "migration_quote_threshold",
        )
        m.raised_quote = self.param(
            ui_amount(state.get("quote_reserve"), m.quote_decimals),
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
        )

        is_migrated = state.get("is_migrated")
        m.migrated = self.param(
            None if is_migrated is None else bool(is_migrated), ValueSource.ONCHAIN_STATE, ctx.slot
        )
        quote_reserve = state.get("quote_reserve")
        if quote_reserve is not None and threshold:
            m.complete = self.param(
                quote_reserve >= threshold,
                ValueSource.DERIVED,
                ctx.slot,
                "quote_reserve >= migration_quote_threshold",
            )

        fees = (config.get("pool_fees") or {}).get("base_fee") or {}
        cliff = fees.get("cliff_fee_numerator")
        if cliff is not None:
            # DBC fee numerators are in 1e9 units.
            m.fee_bps = self.param(
                cliff / 1_000_000_000 * 10_000,
                ValueSource.ONCHAIN_CONFIG,
                cfg_slot,
                "base_fee.cliff_fee_numerator (1e-9 units) as bps",
            )

        option = config.get("migration_option")
        if option is not None:
            m.raw_state = {
                **state,
                "_config_address": config_address,
                "_migration_target": MIGRATION_OPTIONS.get(option, f"unknown({option})"),
            }

        return self.finish(m)
