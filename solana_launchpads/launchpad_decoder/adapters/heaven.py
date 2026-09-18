"""Heaven.

Program: HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn

Heaven does not run a separate curve program: it seeds its own AMM pool with
*virtual* quote liquidity, so a launch trades like a constant-product pool from
block one and never graduates.  Conveniently, `liquidityPoolState` caches
`min/curr/max` price and market cap as f64, which gives the launch price, the
current price and the pool's own high-water mark without any reconstruction --
and the reserve fields let us verify them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext


class HeavenAdapter(Adapter):
    key = "heaven"
    curve_family = CurveFamily.AMM_VIRTUAL_RESERVES

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        m.base_mint = state.get("base_token_mint")
        m.quote_mint = state.get("quote_token_mint") or ctx.spec.default_quote_mint
        m.base_decimals = state.get("base_token_mint_decimals")
        m.quote_decimals = state.get("quote_token_mint_decimals")
        m.creator = state.get("creator")
        m.curve_type = "constant_product_with_virtual_seed"

        base_balance = state.get("base_token_vault_balance")
        quote_balance = state.get("quote_token_vault_balance")
        initial_base = state.get("initial_base_token_vault_balance")
        initial_quote = state.get("initial_quote_token_vault_balance")

        # Supply: the pool was seeded with the whole mint, so the initial base
        # vault balance is the launch supply.
        m.total_supply = self.param(
            ui_amount(initial_base, m.base_decimals),
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
            "initial_base_token_vault_balance",
        )
        if base_balance is not None and initial_base:
            m.tokens_sold = self.param(
                ui_amount(max(0, initial_base - base_balance), m.base_decimals),
                ValueSource.DERIVED,
                ctx.slot,
                "initial_base_token_vault_balance - base_token_vault_balance",
            )
        m.tokens_for_sale = self.param(
            ui_amount(initial_base, m.base_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )

        # The pool caches prices as f64 in quote-per-base whole-token terms.
        for field, target, note in (
            ("min_price", "launch_price_quote", "pool low-water price"),
            ("curr_price", "current_price_quote", "pool cached spot price"),
            ("max_price", "graduation_price_quote", "pool high-water price"),
        ):
            value = state.get(field)
            if value:
                setattr(
                    m,
                    target,
                    self.param(float(value), ValueSource.ONCHAIN_STATE, ctx.slot, note),
                )

        # Recompute from reserves so a stale cached field cannot go unnoticed.
        if initial_base and initial_quote:
            launch_raw = curves.cp_price_raw(initial_quote, initial_base)
            launch_ui = price_raw_to_ui(launch_raw, m.base_decimals, m.quote_decimals)
            if launch_ui is not None:
                if m.launch_price_quote.value is None:
                    m.launch_price_quote = self.param(
                        launch_ui,
                        ValueSource.DERIVED,
                        ctx.slot,
                        "initial_quote_vault / initial_base_vault",
                    )
                elif _diverges(m.launch_price_quote.value, launch_ui):
                    m.warn(
                        f"cached min_price {m.launch_price_quote.value:.3e} disagrees "
                        f"with the seeded reserves {launch_ui:.3e}"
                    )
        if base_balance and quote_balance is not None:
            current_ui = price_raw_to_ui(
                curves.cp_price_raw(quote_balance, base_balance), m.base_decimals, m.quote_decimals
            )
            if m.current_price_quote.value is None and current_ui is not None:
                m.current_price_quote = self.param(
                    current_ui,
                    ValueSource.DERIVED,
                    ctx.slot,
                    "quote_token_vault_balance / base_token_vault_balance",
                )

        for field, target in (
            ("min_mc", "launch_mcap_quote"),
            ("curr_mc", "current_mcap_quote"),
            ("max_mc", "graduation_mcap_quote"),
        ):
            value = state.get(field)
            if value:
                setattr(
                    m,
                    target,
                    self.param(float(value), ValueSource.ONCHAIN_STATE, ctx.slot, f"pool {field}"),
                )

        if quote_balance is not None and initial_quote is not None:
            m.raised_quote = self.param(
                ui_amount(max(0, quote_balance - initial_quote), m.quote_decimals),
                ValueSource.DERIVED,
                ctx.slot,
                "quote vault above the virtual seed",
            )

        m.complete = self.param(
            False, ValueSource.DERIVED, ctx.slot, "Heaven pools never graduate"
        )
        m.migrated = self.param(False, ValueSource.DERIVED, ctx.slot)

        numerator = state.get("swap_fee_numerator")
        denominator = state.get("swap_fee_denominator")
        if numerator is not None and denominator:
            m.fee_bps = self.param(
                numerator / denominator * 10_000,
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                "swap_fee_numerator / swap_fee_denominator",
            )
        return self.finish(m)


def _diverges(a: float, b: float, tolerance: float = 0.02) -> bool:
    if not a or not b:
        return False
    return abs(a - b) / max(abs(a), abs(b)) > tolerance
