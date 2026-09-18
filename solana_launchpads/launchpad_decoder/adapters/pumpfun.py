"""pump.fun bonding curves.

Program: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P

Every launch parameter is program state, not a constant: the single `Global`
account (PDA of ["global"]) holds `initial_virtual_token_reserves`,
`initial_virtual_sol_reserves`, `initial_real_token_reserves` and
`token_total_supply`.  Read those and the launch price, launch market cap and
the 85-SOL-ish raise target all fall out of `x*y=k`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext

GLOBAL_ADDRESS = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"

#: Values read off mainnet `Global` and published in pump-fun/pump-public-docs.
#: Used only when no account source is available; anything sourced from here is
#: tagged BUNDLED_SNAPSHOT so a consumer can tell it was not read from chain.
GLOBAL_SNAPSHOT: Dict[str, int] = {
    "initial_virtual_token_reserves": 1_073_000_000_000_000,
    "initial_virtual_sol_reserves": 30_000_000_000,
    "initial_real_token_reserves": 793_100_000_000_000,
    "token_total_supply": 1_000_000_000_000_000,
    "fee_basis_points": 100,
}


def pick(state: Dict[str, Any], *names: str) -> Optional[Any]:
    """First present, non-None field -- absorbs field renames across versions."""
    for name in names:
        if state.get(name) is not None:
            return state[name]
    return None


class PumpFunAdapter(Adapter):
    key = "pumpfun"
    curve_family = CurveFamily.CONSTANT_PRODUCT_VIRTUAL

    def global_config(self, ctx: DecodeContext):
        name, decoded, slot = ctx.configs.fetch(
            ctx.spec.program_id, GLOBAL_ADDRESS, ctx.schema
        )
        if decoded is not None:
            return decoded, ValueSource.ONCHAIN_CONFIG, slot
        return dict(GLOBAL_SNAPSHOT), ValueSource.BUNDLED_SNAPSHOT, 0

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        cfg, cfg_source, cfg_slot = self.global_config(ctx)

        quote_mint = state.get("quote_mint")
        if not quote_mint or quote_mint == "11111111111111111111111111111111":
            quote_mint = ctx.spec.default_quote_mint
        m.quote_mint = quote_mint
        m.creator = state.get("creator")

        m.base_decimals = ctx.spec.default_base_decimals or 6
        m.quote_decimals = ctx.decimals_for(quote_mint) or 9

        v_base = pick(state, "virtual_token_reserves")
        v_quote = pick(state, "virtual_quote_reserves", "virtual_sol_reserves")
        real_base = pick(state, "real_token_reserves")
        real_quote = pick(state, "real_quote_reserves", "real_sol_reserves")
        total_supply_raw = pick(state, "token_total_supply") or cfg.get("token_total_supply")

        # -- supply -------------------------------------------------------
        m.total_supply = self.param(
            ui_amount(total_supply_raw, m.base_decimals),
            ValueSource.ONCHAIN_STATE if state.get("token_total_supply") else cfg_source,
            ctx.slot,
        )

        init_v_base = cfg.get("initial_virtual_token_reserves")
        init_v_quote = pick(
            cfg, "initial_virtual_sol_reserves", "initial_virtual_quote_reserves"
        )
        init_real_base = cfg.get("initial_real_token_reserves")

        m.tokens_for_sale = self.param(
            ui_amount(init_real_base, m.base_decimals), cfg_source, cfg_slot
        )
        if real_base is not None and init_real_base is not None:
            m.tokens_sold = self.param(
                ui_amount(max(0, init_real_base - real_base), m.base_decimals),
                ValueSource.DERIVED,
                ctx.slot,
                "initial_real_token_reserves - real_token_reserves",
            )

        # -- prices -------------------------------------------------------
        if init_v_base and init_v_quote:
            m.launch_price_quote = self.param(
                price_raw_to_ui(
                    curves.cp_price_raw(init_v_quote, init_v_base),
                    m.base_decimals,
                    m.quote_decimals,
                ),
                cfg_source,
                cfg_slot,
                "initial_virtual_quote / initial_virtual_token",
            )
        if v_base and v_quote is not None:
            m.current_price_quote = self.param(
                price_raw_to_ui(
                    curves.cp_price_raw(v_quote, v_base), m.base_decimals, m.quote_decimals
                ),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                "virtual_quote_reserves / virtual_token_reserves",
            )

        # -- raise target and graduation price ----------------------------
        if init_v_base and init_v_quote and init_real_base:
            target_raw = curves.cp_raise_for_supply(init_v_quote, init_v_base, init_real_base)
            m.raise_target_quote = self.param(
                ui_amount(target_raw, m.quote_decimals),
                ValueSource.DERIVED,
                cfg_slot,
                "quote needed to drain initial_real_token_reserves (pre-fee)",
            )
            final_price_raw = curves.cp_final_price_raw(
                init_v_quote, init_v_base, init_real_base
            )
            m.graduation_price_quote = self.param(
                price_raw_to_ui(final_price_raw, m.base_decimals, m.quote_decimals),
                ValueSource.DERIVED,
                cfg_slot,
                "spot price once real_token_reserves hits zero",
            )

        if real_quote is not None:
            m.raised_quote = self.param(
                ui_amount(real_quote, m.quote_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
            )

        complete = state.get("complete")
        m.complete = self.param(complete, ValueSource.ONCHAIN_STATE, ctx.slot)
        if complete:
            m.migrated = self.param(
                True, ValueSource.DERIVED, ctx.slot, "curve complete -> migrates to PumpSwap"
            )

        fee_bps = cfg.get("fee_basis_points")
        creator_fee = pick(state, "creator_fee_bps") or cfg.get("creator_fee_basis_points")
        if fee_bps is not None:
            m.fee_bps = self.param(
                fee_bps + (creator_fee or 0),
                cfg_source,
                cfg_slot,
                "protocol fee_basis_points + creator fee",
            )

        if state.get("_truncated_fields"):
            m.warn(
                "account predates the current program layout; missing fields: "
                + ", ".join(state["_truncated_fields"])
            )
        if cfg_source is ValueSource.BUNDLED_SNAPSHOT:
            m.warn(
                "Global config was not read from chain -- launch parameters come "
                "from a bundled snapshot and may be stale"
            )
        return self.finish(m)
