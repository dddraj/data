"""Vertigo.

Program: vrTGoBuy5rYSxAfV3jaRJWHH6nN9WK4NRExGxsk1bCJ

A one-sided constant product pool.  `shift` is the virtual quote reserve the
pool is seeded with, and the SDK sets it to the intended initial market cap in
lamports -- which makes the launch market cap literally readable off the pool:

    price     = (shift + token_a_reserves) / token_b_reserves
    launch mc = shift / initial_b_reserves * total_supply = shift

There is no graduation; the pool is the venue.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext


class VertigoAdapter(Adapter):
    key = "vertigo"
    curve_family = CurveFamily.CONSTANT_PRODUCT_VIRTUAL

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        m.quote_mint = state.get("mint_a") or ctx.spec.default_quote_mint
        m.base_mint = state.get("mint_b")
        m.creator = state.get("owner")
        m.curve_type = "one_sided_constant_product"

        m.quote_decimals = ctx.decimals_for(m.quote_mint)
        m.base_decimals = ctx.decimals_for(m.base_mint)

        quote_reserves = state.get("token_a_reserves")
        base_reserves = state.get("token_b_reserves")
        shift = state.get("shift")

        if base_reserves:
            m.current_price_quote = self.param(
                price_raw_to_ui(
                    curves.cp_price_raw((shift or 0) + (quote_reserves or 0), base_reserves),
                    m.base_decimals,
                    m.quote_decimals,
                ),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                "(shift + token_a_reserves) / token_b_reserves",
            )

        # The pool is seeded with the entire mint, so
        # base_reserves + tokens_sold == launch supply.  Without a trade history
        # the best available proxy for launch supply is the current base
        # reserve; when the pool is untouched the two coincide.
        if base_reserves:
            k = ((shift or 0) + (quote_reserves or 0)) * base_reserves
            initial_base = k // shift if shift else None
            if initial_base:
                m.total_supply = self.param(
                    ui_amount(initial_base, m.base_decimals),
                    ValueSource.DERIVED,
                    ctx.slot,
                    "k / shift -- the base reserve the pool was seeded with",
                )
                m.tokens_for_sale = m.total_supply
                m.tokens_sold = self.param(
                    ui_amount(max(0, initial_base - base_reserves), m.base_decimals),
                    ValueSource.DERIVED,
                    ctx.slot,
                )
                m.launch_price_quote = self.param(
                    price_raw_to_ui(
                        curves.cp_price_raw(shift, initial_base),
                        m.base_decimals,
                        m.quote_decimals,
                    ),
                    ValueSource.DERIVED,
                    ctx.slot,
                    "shift / seeded base reserve",
                )

        if quote_reserves is not None:
            m.raised_quote = self.param(
                ui_amount(quote_reserves, m.quote_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
            )
        if shift is not None and m.quote_decimals is not None:
            m.launch_mcap_quote = self.param(
                ui_amount(shift, m.quote_decimals),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                "`shift` is set to the intended launch market cap",
            )

        m.complete = self.param(
            False, ValueSource.DERIVED, ctx.slot, "Vertigo pools do not graduate"
        )
        m.migrated = self.param(False, ValueSource.DERIVED, ctx.slot)

        fee_params = state.get("fee_params") or {}
        royalties = state.get("royalties")
        if royalties is not None:
            m.fee_bps = self.param(
                royalties / 100.0 if royalties < 10_000 else royalties,
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                "pool royalties field (units are pool-specific)",
            )
        if fee_params:
            m.raw_state = {**state, "_fee_params": fee_params}
        m.warn(
            "Vertigo has no graduation target, so raise_target_quote and "
            "graduation_price_quote are intentionally unset"
        )
        return self.finish(m)
