"""Raydium LaunchLab pools (LetsBonk.fun, Cook.meme, Raydium's own UI, ...).

Program: LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj

LaunchLab is multi-tenant, and everything needed is on the pool itself:
`supply`, `total_base_sell`, `total_quote_fund_raising`, the virtual/real
reserve pair, plus pointers to the `GlobalConfig` (which curve type this pool
uses) and the `PlatformConfig` (which platform launched it).

The three curve types are implemented exactly as `raydium-sdk-V2` does:

* constant product -- price = (virtualB + realB) / (virtualA - realA)
* fixed price      -- price = virtualB / virtualA, flat for the whole curve
* linear           -- price = virtualA * realA / 2^64  (virtualA is the slope)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext

CURVE_TYPES = {0: "constant_product", 1: "fixed_price", 2: "linear"}
FAMILY_BY_CURVE = {
    0: CurveFamily.CONSTANT_PRODUCT_VIRTUAL,
    1: CurveFamily.FIXED_PRICE,
    2: CurveFamily.LINEAR,
}
POOL_STATUS = {0: "funding", 1: "awaiting_migration", 2: "migrated"}

#: `trade_fee_rate` and friends are denominated in hundredths of a bip (1e-6)
FEE_RATE_DENOMINATOR = 1_000_000


class RaydiumLaunchLabAdapter(Adapter):
    key = "raydium_launchlab"

    def _configs(self, ctx: DecodeContext, state: Dict[str, Any]) -> Tuple[dict, dict, int]:
        global_cfg: dict = {}
        platform_cfg: dict = {}
        slot = 0
        for field, target in (("global_config", "g"), ("platform_config", "p")):
            address = state.get(field)
            if not address:
                continue
            name, decoded, cfg_slot = ctx.configs.fetch(
                ctx.spec.program_id, address, ctx.schema
            )
            if decoded is None:
                continue
            slot = max(slot, cfg_slot)
            if target == "g":
                global_cfg = decoded
            else:
                platform_cfg = decoded
        return global_cfg, platform_cfg, slot

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        global_cfg, platform_cfg, cfg_slot = self._configs(ctx, state)

        m.base_mint = state.get("base_mint")
        m.quote_mint = state.get("quote_mint") or ctx.spec.default_quote_mint
        m.creator = state.get("creator")
        m.base_decimals = state.get("base_decimals")
        m.quote_decimals = state.get("quote_decimals")
        if m.base_decimals is None:
            m.base_decimals = ctx.decimals_for(m.base_mint)
        if m.quote_decimals is None:
            m.quote_decimals = ctx.decimals_for(m.quote_mint)

        curve_type = global_cfg.get("curve_type")
        if curve_type is None:
            curve_type = 0
            m.warn("GlobalConfig unavailable; assuming constant-product curve_type=0")
        m.curve_type = CURVE_TYPES.get(curve_type, f"unknown({curve_type})")
        m.curve_family = FAMILY_BY_CURVE.get(curve_type, CurveFamily.UNKNOWN)

        supply = state.get("supply")
        total_base_sell = state.get("total_base_sell")
        total_raise = state.get("total_quote_fund_raising")
        virtual_a = state.get("virtual_base")
        virtual_b = state.get("virtual_quote")
        real_a = state.get("real_base") or 0
        real_b = state.get("real_quote") or 0
        migrate_fee = state.get("migrate_fee") or global_cfg.get("migrate_fee") or 0
        vesting = state.get("vesting_schedule") or {}
        locked = vesting.get("total_locked_amount") or 0

        m.total_supply = self.param(
            ui_amount(supply, m.base_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )
        m.tokens_for_sale = self.param(
            ui_amount(total_base_sell, m.base_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )
        m.tokens_sold = self.param(
            ui_amount(real_a, m.base_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )
        m.raise_target_quote = self.param(
            ui_amount(total_raise, m.quote_decimals),
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
            "total_quote_fund_raising",
        )
        m.raised_quote = self.param(
            ui_amount(real_b, m.quote_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )

        # -- prices, per curve type ----------------------------------------
        launch_raw = current_raw = None
        if curve_type == 0 and virtual_a and virtual_b:
            launch_raw = curves.cp_price_raw(virtual_b, virtual_a)
            denominator = virtual_a - real_a
            current_raw = curves.cp_price_raw(virtual_b + real_b, denominator)
        elif curve_type == 1 and virtual_a and virtual_b:
            launch_raw = curves.fixed_price_raw(virtual_b, virtual_a)
            current_raw = launch_raw
        elif curve_type == 2 and virtual_a:
            launch_raw = 0.0  # a linear curve starts at price zero
            current_raw = curves.launchlab_linear_price_raw(virtual_a, real_a)

        m.launch_price_quote = self.param(
            price_raw_to_ui(launch_raw, m.base_decimals, m.quote_decimals),
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
            f"{m.curve_type} initial price",
        )
        m.current_price_quote = self.param(
            price_raw_to_ui(current_raw, m.base_decimals, m.quote_decimals),
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
        )

        # Graduation price is the price the migrated AMM pool opens at:
        # (raise - migrate fee) spread over the tokens reserved for migration.
        if supply is not None and total_base_sell is not None and total_raise is not None:
            migrate_tokens = supply - total_base_sell - locked
            if migrate_tokens > 0:
                end_raw = (total_raise - migrate_fee) / migrate_tokens
                m.graduation_price_quote = self.param(
                    price_raw_to_ui(end_raw, m.base_decimals, m.quote_decimals),
                    ValueSource.DERIVED,
                    ctx.slot,
                    "(total_quote_fund_raising - migrate_fee) / (supply - total_base_sell - locked)",
                )

        status = state.get("status")
        m.complete = self.param(
            None if status is None else status >= 1, ValueSource.ONCHAIN_STATE, ctx.slot,
            POOL_STATUS.get(status, ""),
        )
        m.migrated = self.param(
            None if status is None else status == 2, ValueSource.ONCHAIN_STATE, ctx.slot
        )

        trade_fee = global_cfg.get("trade_fee_rate")
        platform_fee = platform_cfg.get("fee_rate")
        creator_fee = platform_cfg.get("creator_fee_rate")
        rates = [r for r in (trade_fee, platform_fee, creator_fee) if r is not None]
        if rates:
            m.fee_bps = self.param(
                sum(rates) / FEE_RATE_DENOMINATOR * 10_000,
                ValueSource.ONCHAIN_CONFIG,
                cfg_slot,
                "trade + platform + creator fee rates (1e-6 units) as bps",
            )

        platform_name = _platform_name(platform_cfg)
        if platform_name:
            m.raw_state = {**state, "_platform_name": platform_name}

        return self.finish(m)


def _platform_name(platform_cfg: dict) -> str:
    raw = platform_cfg.get("name")
    if isinstance(raw, (list, tuple)):
        try:
            return bytes(int(b) for b in raw).rstrip(b"\x00").decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return raw if isinstance(raw, str) else ""
