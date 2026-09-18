"""Per-launchpad PDA derivations.

Each of these is verified against a published constant in `tests/test_pdas.py`,
so a wrong seed order fails loudly rather than returning a plausible address.
"""

from __future__ import annotations

from typing import Tuple

from .base58 import b58decode
from .pubkey import find_program_address
from .registry import WSOL

PUMPFUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
METEORA_DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
MOONIT = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"
#: pump.fun's fee tiers live in a separate program
PUMP_FEES = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"


# -- pump.fun --------------------------------------------------------------


def pumpfun_global() -> str:
    """The single `Global` account holding every launch parameter."""
    return find_program_address([b"global"], PUMPFUN)[0]


def pumpfun_bonding_curve(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMPFUN)[0]


def pumpfun_fee_config() -> str:
    """`["fee_config", pump_program_id]` -- but owned by the pump-fees program."""
    return find_program_address([b"fee_config", b58decode(PUMPFUN)], PUMP_FEES)[0]


# -- Raydium LaunchLab -----------------------------------------------------


def launchlab_global_config(
    quote_mint: str = WSOL, curve_type: int = 0, index: int = 0
) -> str:
    """`["global_config", quote_mint, u8(curve_type), u16_be(index)]`."""
    return find_program_address(
        [
            b"global_config",
            b58decode(quote_mint),
            bytes([curve_type]),
            index.to_bytes(2, "big"),
        ],
        LAUNCHLAB,
    )[0]


def launchlab_platform_config(platform_admin: str) -> str:
    """`["platform_config", platform_admin_wallet]` -- one per launchpad tenant."""
    return find_program_address([b"platform_config", b58decode(platform_admin)], LAUNCHLAB)[0]


def launchlab_pool(base_mint: str, quote_mint: str = WSOL) -> str:
    return find_program_address(
        [b"pool", b58decode(base_mint), b58decode(quote_mint)], LAUNCHLAB
    )[0]


def launchlab_vault(pool: str, mint: str) -> str:
    return find_program_address([b"pool_vault", b58decode(pool), b58decode(mint)], LAUNCHLAB)[0]


# -- Meteora DBC -----------------------------------------------------------


def dbc_virtual_pool(config: str, base_mint: str, quote_mint: str = WSOL) -> Tuple[str, str]:
    """DBC orders the two mints by their raw bytes before seeding the pool."""
    base_raw, quote_raw = b58decode(base_mint), b58decode(quote_mint)
    first, second = (base_raw, quote_raw) if base_raw > quote_raw else (quote_raw, base_raw)
    address, _ = find_program_address([b"pool", b58decode(config), first, second], METEORA_DBC)
    return address, config


def dbc_token_vault(pool: str, mint: str) -> str:
    return find_program_address([b"token_vault", b58decode(mint), b58decode(pool)], METEORA_DBC)[0]


# -- Moonit ----------------------------------------------------------------


def moonit_curve(mint: str) -> str:
    """`["token", mint]` -- per `getCurveAccount` in the Moonit SDK."""
    return find_program_address([b"token", b58decode(mint)], MOONIT)[0]


# -- PumpSwap --------------------------------------------------------------


def pumpswap_global_config() -> str:
    return find_program_address([b"global_config"], PUMPSWAP)[0]


def pumpswap_pool(index: int, creator: str, base_mint: str, quote_mint: str = WSOL) -> str:
    return find_program_address(
        [
            b"pool",
            index.to_bytes(2, "little"),
            b58decode(creator),
            b58decode(base_mint),
            b58decode(quote_mint),
        ],
        PUMPSWAP,
    )[0]
