"""Canonical, cross-launchpad result types.

Every launchpad is reduced to the same handful of numbers so that a consumer
never has to know whose curve it is looking at:

* what one token cost at the very first buy (`launch_price_quote`),
* what it costs now (`current_price_quote`),
* what it will cost at graduation (`graduation_price_quote`),
* the token supply,
* the resulting market caps,
* and how much quote the curve is trying to raise (`raise_target_quote`).

Each number carries a `ValueSource`, because "where did this come from" is the
difference between a value that tracks a program upgrade automatically and one
that silently goes stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ValueSource(str, Enum):
    #: read out of the per-launch curve/pool account itself
    ONCHAIN_STATE = "onchain_state"
    #: read out of the program's global / platform / per-partner config account
    ONCHAIN_CONFIG = "onchain_config"
    #: layout came from the IDL the program publishes on chain
    ONCHAIN_IDL = "onchain_idl"
    #: layout came from an IDL snapshot bundled with this repo
    BUNDLED_IDL = "bundled_idl"
    #: constant captured from the program source / official SDK, not from chain
    BUNDLED_SNAPSHOT = "bundled_snapshot"
    #: computed from other values above
    DERIVED = "derived"
    #: could not be determined
    UNAVAILABLE = "unavailable"


@dataclass
class Param:
    """A value plus where it came from and when."""

    value: Any
    source: ValueSource = ValueSource.UNAVAILABLE
    slot: int = 0
    note: str = ""

    def __bool__(self) -> bool:
        return self.value is not None

    def as_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "source": self.source.value, "slot": self.slot, "note": self.note}


class CurveFamily(str, Enum):
    #: x*y=k over virtual reserves (pump.fun, LaunchLab constant, Moonit CP, Vertigo)
    CONSTANT_PRODUCT_VIRTUAL = "constant_product_virtual"
    #: price grows linearly in tokens sold (LaunchLab linear, Moonit LinearV1)
    LINEAR = "linear"
    #: one price for the whole curve (LaunchLab fixed, Moonit flat)
    FIXED_PRICE = "fixed_price"
    #: concentrated-liquidity style piecewise sqrt-price curve (Meteora DBC)
    SQRT_PIECEWISE = "sqrt_piecewise"
    #: price = (raised + c)^e / scale (GoFundMeme)
    POWER_LAW = "power_law"
    #: constant-product AMM seeded with virtual reserves, no graduation (Heaven)
    AMM_VIRTUAL_RESERVES = "amm_virtual_reserves"
    #: plain AMM over real vault balances (PumpSwap and other post-graduation pools)
    AMM_REAL_RESERVES = "amm_real_reserves"
    UNKNOWN = "unknown"


@dataclass
class LaunchMetrics:
    """The unified answer for one launch, whatever program produced it."""

    launchpad: str
    program_id: str
    curve_address: Optional[str] = None
    curve_account_type: str = ""
    base_mint: Optional[str] = None
    quote_mint: Optional[str] = None
    base_decimals: Optional[int] = None
    quote_decimals: Optional[int] = None
    curve_family: CurveFamily = CurveFamily.UNKNOWN
    curve_type: str = ""

    # supply -------------------------------------------------------------
    total_supply: Param = field(default_factory=lambda: Param(None))
    tokens_for_sale: Param = field(default_factory=lambda: Param(None))
    tokens_sold: Param = field(default_factory=lambda: Param(None))

    # price (quote token per whole base token) -----------------------------
    launch_price_quote: Param = field(default_factory=lambda: Param(None))
    current_price_quote: Param = field(default_factory=lambda: Param(None))
    graduation_price_quote: Param = field(default_factory=lambda: Param(None))

    # market cap (quote token, price x total supply) -----------------------
    launch_mcap_quote: Param = field(default_factory=lambda: Param(None))
    current_mcap_quote: Param = field(default_factory=lambda: Param(None))
    graduation_mcap_quote: Param = field(default_factory=lambda: Param(None))

    # the raise -----------------------------------------------------------
    raise_target_quote: Param = field(default_factory=lambda: Param(None))
    raised_quote: Param = field(default_factory=lambda: Param(None))
    progress: Param = field(default_factory=lambda: Param(None))

    # status --------------------------------------------------------------
    complete: Param = field(default_factory=lambda: Param(None))
    migrated: Param = field(default_factory=lambda: Param(None))
    creator: Optional[str] = None
    fee_bps: Param = field(default_factory=lambda: Param(None))

    # bookkeeping ----------------------------------------------------------
    slot: int = 0
    schema_source: str = ""
    schema_fingerprint: str = ""
    program_revision: Optional[tuple] = None
    raw_state: Dict[str, Any] = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    # -- helpers ----------------------------------------------------------
    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def mcap_in(self, quote_price_usd: float) -> Dict[str, Optional[float]]:
        """Convert the three market caps into USD given a quote-token price."""

        def conv(param: Param) -> Optional[float]:
            return None if param.value is None else float(param.value) * quote_price_usd

        return {
            "launch_mcap_usd": conv(self.launch_mcap_quote),
            "current_mcap_usd": conv(self.current_mcap_quote),
            "graduation_mcap_usd": conv(self.graduation_mcap_quote),
        }

    def as_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "launchpad": self.launchpad,
            "program_id": self.program_id,
            "curve_address": self.curve_address,
            "curve_account_type": self.curve_account_type,
            "base_mint": self.base_mint,
            "quote_mint": self.quote_mint,
            "base_decimals": self.base_decimals,
            "quote_decimals": self.quote_decimals,
            "curve_family": self.curve_family.value,
            "curve_type": self.curve_type,
            "creator": self.creator,
            "slot": self.slot,
            "schema_source": self.schema_source,
            "schema_fingerprint": self.schema_fingerprint,
            "program_revision": list(self.program_revision) if self.program_revision else None,
            "warnings": list(self.warnings),
        }
        for name in (
            "total_supply",
            "tokens_for_sale",
            "tokens_sold",
            "launch_price_quote",
            "current_price_quote",
            "graduation_price_quote",
            "launch_mcap_quote",
            "current_mcap_quote",
            "graduation_mcap_quote",
            "raise_target_quote",
            "raised_quote",
            "progress",
            "complete",
            "migrated",
            "fee_bps",
        ):
            out[name] = getattr(self, name).as_dict()
        if include_raw:
            out["raw_state"] = self.raw_state
        return out


def ui_amount(raw: Optional[int], decimals: Optional[int]) -> Optional[float]:
    """Raw base units -> whole tokens."""
    if raw is None or decimals is None:
        return None
    return raw / (10**decimals)


def price_raw_to_ui(
    raw_price: Optional[float], base_decimals: Optional[int], quote_decimals: Optional[int]
) -> Optional[float]:
    """Quote-raw-per-base-raw -> quote-whole-per-base-whole."""
    if raw_price is None or base_decimals is None or quote_decimals is None:
        return None
    return raw_price * (10 ** (base_decimals - quote_decimals))
