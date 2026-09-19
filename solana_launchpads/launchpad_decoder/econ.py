"""Where each launchpad's curve economics actually live.

A warehouse that wants a progress bar needs two numbers per curve: how much
quote has been raised, and how much it takes to graduate. The obvious way to
store that is a table keyed on `program_id`. **That only works for pump.fun**,
and shipping it for the others produces confident wrong numbers.

The reason is scope. pump.fun holds its launch parameters in one `Global`
account, so every coin on the program shares them and a per-program row is
exactly right. No other launchpad does that:

* **Raydium LaunchLab** writes `supply`, `total_base_sell` and
  `total_quote_fund_raising` onto each `PoolState`. A LetsBonk coin and a
  Cook.meme coin on the same program have different raise targets, so there is
  no per-program number to store -- and none is needed, because the target is a
  field on the pool row an indexer already has.
* **Meteora DBC** holds them on a `PoolConfig` that each partner creates, so
  they are per *config*: Believe, Bags and Jupiter Studio each have their own,
  and one program serves all of them.
* **Moonit** and **GoFundMeme** are per curve, like LaunchLab.
* **Heaven**, **Vertigo** and **PumpSwap** have no graduation at all, so there
  is no raise target and a progress bar is meaningless. Null is the right
  answer, not a borrowed number.

So this module records, per launchpad, the *scope* at which the economics are
fixed and which two fields divide to give progress. Three of the launchpads
need no table at all once you know which field to read.

Everything here is a field name or a scope, not a value. Values are read from
the chain by the decoder; the only constants are pump.fun's, and those are the
bundled snapshot in `adapters/pumpfun.py`, tagged as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: Where a launchpad's raise target is fixed.
#:
#: ``program``  one account serves every coin -- a per-program row is correct
#: ``config``   one account per partner/platform -- key on (program, config)
#: ``pool``     the numbers are on the curve account itself -- no table needed
#: ``none``     the program has no graduation, so there is no raise target
SCOPES = ("program", "config", "pool", "none")


@dataclass(frozen=True)
class CurveEcon:
    """How to compute curve progress for one launchpad."""

    launchpad: str
    program_id: str
    scope: str
    #: account type holding the raise target, at `scope`
    source_account: Optional[str] = None
    #: field holding quote raised so far, on the curve/pool account
    raised_field: Optional[str] = None
    #: field holding the quote needed to graduate
    target_field: Optional[str] = None
    #: field holding the tokens the curve will sell
    tokens_for_sale_field: Optional[str] = None
    #: opening virtual reserves, where the curve family has them
    initial_quote_field: Optional[str] = None
    initial_base_field: Optional[str] = None
    #: True when the target is computed rather than stored
    target_is_derived: bool = False
    note: str = ""

    @property
    def needs_a_table(self) -> bool:
        """False when an indexer can read progress straight off the pool row."""
        return self.scope in ("program", "config")

    @property
    def has_progress(self) -> bool:
        return self.scope != "none"

    @property
    def progress_is_stored(self) -> bool:
        """True when progress is a plain division of two stored fields.

        False means the numbers have to come out of the curve maths -- which is
        what the decoder is for. Dividing whatever two fields look closest is
        how a progress bar ends up confidently wrong.
        """
        return bool(self.raised_field and self.target_field)


#: Keyed by launchpad key. Program ids are repeated so a consumer can join on
#: either without reaching back into the registry.
ECON: Tuple[CurveEcon, ...] = (
    CurveEcon(
        launchpad="pumpfun",
        program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        scope="program",
        source_account="Global",
        raised_field="real_quote_reserves",
        target_field=None,
        tokens_for_sale_field="initial_real_token_reserves",
        initial_quote_field="initial_virtual_sol_reserves",
        initial_base_field="initial_virtual_token_reserves",
        target_is_derived=True,
        note=(
            "The one launchpad where a per-program row is right. Note it seeds "
            "TWO openings and the curve account does not say which: 30 SOL for "
            "SOL-quoted coins, initial_virtual_quote_reserves otherwise. Read it "
            "off the curve as virtual_quote - real_quote, which is exact and "
            "fixed for the curve's life -- see docs/CURVE_MATH.md. Store one row "
            "per variant, not one per program."
        ),
    ),
    CurveEcon(
        launchpad="raydium_launchlab",
        program_id="LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
        scope="pool",
        source_account="PoolState",
        raised_field="real_quote",
        target_field="total_quote_fund_raising",
        tokens_for_sale_field="total_base_sell",
        initial_quote_field="virtual_quote",
        initial_base_field="virtual_base",
        note=(
            "Everything is on the pool, so no table is needed: progress is "
            "real_quote / total_quote_fund_raising, both fields of the row you "
            "already decode. A per-program row would be wrong -- each platform "
            "sizes its own raise."
        ),
    ),
    CurveEcon(
        launchpad="meteora_dbc",
        program_id="dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",
        scope="config",
        source_account="PoolConfig",
        raised_field="quote_reserve",
        target_field="migration_quote_threshold",
        tokens_for_sale_field="swap_base_amount",
        initial_quote_field="sqrt_start_price",
        initial_base_field=None,
        note=(
            "Per partner, not per program: Believe, Bags and Jupiter Studio each "
            "create their own PoolConfig on the same program. Key the table on "
            "(program_id, config) -- VirtualPool.config points at the row. The "
            "curve is a sqrt-price ladder, so initial_quote is sqrt_start_price "
            "in Q64.64 and there is no initial_base; square it for a price."
        ),
    ),
    CurveEcon(
        launchpad="moonit",
        program_id="MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
        scope="pool",
        source_account="CurveAccount",
        raised_field=None,
        target_field=None,
        tokens_for_sale_field="total_supply",
        target_is_derived=True,
        note=(
            "Stores NO raised amount and NO raise target, so there is nothing "
            "to divide. What it has is curve_amount -- tokens still ON the "
            "curve -- so position is total_supply - curve_amount, and both the "
            "raise and what has been raised come out of the curve maths. Take "
            "raised_quote / raise_target_quote off the decoder's output. Also "
            "note marketcap_threshold is a MARKET CAP in Moonit's own sense of "
            "it (price x tokens SOLD, not x total supply); comparing it against "
            "another launchpad's raise target is wrong by the fraction of "
            "supply sold."
        ),
    ),
    CurveEcon(
        launchpad="gofundmeme",
        program_id="GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7",
        scope="pool",
        source_account="BondingCurvePool",
        raised_field="current_sol",
        target_field="target_raise",
        note=(
            "Per pool, and stored outright: progress is current_sol / "
            "target_raise. Older pools carry the raised amount as total_raised "
            "instead, so read current_sol and fall back to it."
        ),
    ),
    CurveEcon(
        launchpad="heaven",
        program_id="HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn",
        scope="none",
        note=(
            "An AMM seeded with virtual quote liquidity, not a bonding curve "
            "that graduates. There is no raise target, so progress is null -- "
            "which is the honest answer, not a gap to fill."
        ),
    ),
    CurveEcon(
        launchpad="vertigo",
        program_id="vrTGoBuy5rYSxAfV3jaRJWHH6nN9WK4NRExGxsk1bCJ",
        scope="none",
        note="Constant-product pool with no graduation step, so no raise target.",
    ),
    CurveEcon(
        launchpad="pumpswap",
        program_id="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        scope="none",
        note=(
            "The destination pump.fun curves migrate INTO, not a curve. A coin "
            "with a PumpSwap pool has finished its curve; progress is 100% by "
            "definition, or null if you only show bars for live curves."
        ),
    ),
)

_BY_PROGRAM: Dict[str, CurveEcon] = {row.program_id: row for row in ECON}
_BY_KEY: Dict[str, CurveEcon] = {row.launchpad: row for row in ECON}


def for_program(program_id: str) -> Optional[CurveEcon]:
    return _BY_PROGRAM.get(program_id)


def for_launchpad(key: str) -> Optional[CurveEcon]:
    return _BY_KEY.get(key)
