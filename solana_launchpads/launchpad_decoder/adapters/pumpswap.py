"""PumpSwap -- where a completed pump.fun curve lands.

Program: pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA

Included so that a coin's price and market cap keep coming from one pipeline
after graduation.  The `Pool` account holds only pointers, so pricing needs the
two vault token-account balances; `virtual_quote_reserves` (non-zero only for
"boost" pools) is added to the quote side exactly as the program does.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext


class PumpSwapAdapter(Adapter):
    key = "pumpswap"
    curve_family = CurveFamily.AMM_REAL_RESERVES

    def _vault_balance(self, ctx: DecodeContext, token_account: Optional[str]) -> Optional[int]:
        if not token_account or ctx.source is None:
            return None
        getter = getattr(ctx.source, "get_token_account_balance", None)
        if callable(getter):
            try:
                value = getter(token_account)
            except Exception:  # noqa: BLE001
                value = None
            if value and "amount" in value:
                return int(value["amount"])
        account = ctx.source.get_account(token_account)
        if account is not None and len(account.data) >= 72:
            # SPL token account layout: amount is a u64 at offset 64
            return int.from_bytes(account.data[64:72], "little")
        return None

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        m.base_mint = state.get("base_mint")
        m.quote_mint = state.get("quote_mint") or ctx.spec.default_quote_mint
        m.creator = state.get("coin_creator") or state.get("creator")
        m.curve_type = "constant_product_amm"

        m.base_decimals = ctx.decimals_for(m.base_mint) or ctx.spec.default_base_decimals
        m.quote_decimals = ctx.decimals_for(m.quote_mint) or 9

        base_balance = self._vault_balance(ctx, state.get("pool_base_token_account"))
        quote_balance = self._vault_balance(ctx, state.get("pool_quote_token_account"))
        virtual_quote = state.get("virtual_quote_reserves") or 0

        if base_balance and quote_balance is not None:
            m.current_price_quote = self.param(
                price_raw_to_ui(
                    curves.cp_price_raw(quote_balance + virtual_quote, base_balance),
                    m.base_decimals,
                    m.quote_decimals,
                ),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                "(quote vault + virtual_quote_reserves) / base vault",
            )
        else:
            m.warn(
                "pool vault balances were not available; PumpSwap pricing needs "
                "pool_base_token_account and pool_quote_token_account"
            )

        supply = None
        if ctx.source is not None and m.base_mint:
            getter = getattr(ctx.source, "get_token_supply", None)
            if callable(getter):
                try:
                    value = getter(m.base_mint)
                except Exception:  # noqa: BLE001
                    value = None
                if value and "amount" in value:
                    supply = int(value["amount"])
        m.total_supply = self.param(
            ui_amount(supply, m.base_decimals),
            ValueSource.ONCHAIN_STATE if supply else ValueSource.UNAVAILABLE,
            ctx.slot,
            "SPL mint supply",
        )

        m.complete = self.param(True, ValueSource.DERIVED, ctx.slot, "already an AMM pool")
        m.migrated = self.param(True, ValueSource.DERIVED, ctx.slot)
        return self.finish(m)
