"""The launchpad registry.

Two layers, and the distinction matters more than the list itself:

**Programs.** A handful of on-chain programs carry essentially all Solana
bonding-curve launch volume.  Each entry below pins a program id, the account
that holds per-launch curve state, the account(s) that hold the launch
*parameters*, and which curve family the program implements.

**Platforms.** Several of those programs are multi-tenant: Raydium LaunchLab
sells curves to any platform that creates a `PlatformConfig`, and Meteora DBC
does the same with `PoolConfig`.  LetsBonk, Believe, Bags, Jupiter Studio and
dozens of others are *rows in those programs' config accounts*, not separate
programs.  So the registry does not try to enumerate them by hand --
`discover_platforms()` reads them off the chain, which is the only way that
list stays correct.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .anchor_idl import ProgramSchema, compile_idl
from .types import CurveFamily

IDL_DIR = os.path.join(os.path.dirname(__file__), "idl")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass(frozen=True)
class ConfigAccount:
    """A program account holding launch parameters rather than launch state."""

    role: str
    account_type: str
    #: fixed address when the program has exactly one; otherwise PDA seeds
    address: Optional[str] = None
    seeds: Optional[Tuple] = None
    #: True when each platform/partner gets its own instance of this account
    per_platform: bool = False


@dataclass(frozen=True)
class LaunchpadSpec:
    key: str
    display_name: str
    program_id: str
    curve_family: CurveFamily
    #: IDL account name of the per-launch curve/pool state
    state_account: str
    adapter: str
    idl_file: Optional[str] = None
    config_accounts: Tuple[ConfigAccount, ...] = ()
    default_quote_mint: str = WSOL
    default_base_decimals: Optional[int] = None
    graduates_to: Optional[str] = None
    #: platforms known to sit on top of this program (informational only --
    #: `discover_platforms()` is the authoritative source)
    known_platforms: Tuple[str, ...] = ()
    docs: str = ""
    notes: str = ""
    #: True when the layout must be learned from the program's on-chain IDL
    requires_onchain_idl: bool = False

    def bundled_schema(self) -> Optional[ProgramSchema]:
        if not self.idl_file:
            return None
        path = os.path.join(IDL_DIR, self.idl_file)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        schema = compile_idl(document, source=f"bundled:{self.idl_file}")
        if schema.program_id is None:
            schema.program_id = self.program_id
        return schema


LAUNCHPADS: Tuple[LaunchpadSpec, ...] = (
    LaunchpadSpec(
        key="pumpfun",
        display_name="Pump.fun",
        program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        curve_family=CurveFamily.CONSTANT_PRODUCT_VIRTUAL,
        state_account="BondingCurve",
        adapter="pumpfun",
        idl_file="pump.json",
        config_accounts=(
            ConfigAccount(
                "global",
                "Global",
                address="4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
                seeds=("global",),
            ),
        ),
        default_base_decimals=6,
        graduates_to="pumpswap",
        docs="https://github.com/pump-fun/pump-public-docs",
        notes=(
            "Launch parameters live entirely in the single Global account: "
            "initial_virtual_token_reserves, initial_virtual_sol_reserves, "
            "initial_real_token_reserves and token_total_supply. Change those "
            "on chain and every future launch changes with them."
        ),
    ),
    LaunchpadSpec(
        key="pumpswap",
        display_name="PumpSwap (pump.fun graduation AMM)",
        program_id="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        curve_family=CurveFamily.AMM_REAL_RESERVES,
        state_account="Pool",
        adapter="pumpswap",
        idl_file="pump_amm.json",
        config_accounts=(
            ConfigAccount("global_config", "GlobalConfig", seeds=("global_config",)),
        ),
        default_base_decimals=6,
        docs="https://github.com/pump-fun/pump-public-docs",
        notes=(
            "Not a bonding curve: this is where a pump.fun coin lands after the "
            "curve completes. Included so post-graduation price and market cap "
            "come from the same pipeline."
        ),
    ),
    LaunchpadSpec(
        key="raydium_launchlab",
        display_name="Raydium LaunchLab",
        program_id="LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
        curve_family=CurveFamily.CONSTANT_PRODUCT_VIRTUAL,
        state_account="PoolState",
        adapter="raydium_launchlab",
        idl_file="raydium_launchpad.json",
        config_accounts=(
            # ["global_config", quote_mint, u8(curve_type), u16_be(index)] --
            # index 0 / curve_type 0 / wSOL is the default constant-product config
            # that LetsBonk.fun and most other tenants use.
            ConfigAccount(
                "global_config",
                "GlobalConfig",
                address="6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX",
                per_platform=False,
            ),
            # ["platform_config", platform_admin_wallet] -- one per tenant
            ConfigAccount("platform_config", "PlatformConfig", per_platform=True),
        ),
        graduates_to="raydium_cpmm_or_amm",
        known_platforms=("LetsBonk.fun", "Cook.meme", "Raydium LaunchLab UI"),
        docs="https://docs.raydium.io/products/launchlab",
        notes=(
            "Multi-tenant. curve_type comes from GlobalConfig "
            "(0=constant product, 1=fixed price, 2=linear), and every pool "
            "carries its own supply / total_base_sell / total_quote_fund_raising."
        ),
    ),
    LaunchpadSpec(
        key="meteora_dbc",
        display_name="Meteora Dynamic Bonding Curve",
        program_id="dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",
        curve_family=CurveFamily.SQRT_PIECEWISE,
        state_account="VirtualPool",
        adapter="meteora_dbc",
        idl_file="meteora_dbc.json",
        config_accounts=(
            ConfigAccount("pool_config", "PoolConfig", per_platform=True),
        ),
        graduates_to="meteora_damm_v1_or_v2",
        known_platforms=("Believe", "Jupiter Studio", "Bags", "Moonit (DBC configs)"),
        docs="https://github.com/MeteoraAg/dynamic-bonding-curve",
        notes=(
            "The most configurable of the lot: each partner creates a PoolConfig "
            "holding up to 20 (sqrt_price, liquidity) segments, sqrt_start_price, "
            "migration_quote_threshold and the pre/post migration supply. Read the "
            "config and you have the launch price, supply and raise target exactly."
        ),
    ),
    LaunchpadSpec(
        key="moonit",
        display_name="Moonit (formerly Moonshot / DEX Screener)",
        program_id="MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
        curve_family=CurveFamily.CONSTANT_PRODUCT_VIRTUAL,
        state_account="CurveAccount",
        adapter="moonit",
        idl_file="moonit.json",
        config_accounts=(
            ConfigAccount("config", "ConfigAccount", seeds=("config",)),
        ),
        default_base_decimals=9,
        graduates_to="raydium_or_meteora",
        docs="https://github.com/gomoonit/moonit-sdk",
        notes=(
            "Sizes the curve from a market-cap threshold rather than a quote "
            "target, so the raise target has to be solved for. Five curve types: "
            "LinearV1, ConstantProductV1/V2, FlatCurveV1, FlatCurveV1AntiSnipe."
        ),
    ),
    LaunchpadSpec(
        key="heaven",
        display_name="Heaven",
        program_id="HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn",
        curve_family=CurveFamily.AMM_VIRTUAL_RESERVES,
        state_account="liquidityPoolState",
        adapter="heaven",
        idl_file="heaven_amm.json",
        config_accounts=(
            ConfigAccount("protocol_config", "protocolConfig", per_platform=False),
        ),
        docs="https://www.npmjs.com/package/heaven-sdk",
        notes=(
            "No separate curve program: Heaven seeds its own AMM pool with "
            "virtual quote liquidity, so there is no graduation. The pool state "
            "stores min/curr/max price and market cap as f64 directly."
        ),
    ),
    LaunchpadSpec(
        key="vertigo",
        display_name="Vertigo",
        program_id="vrTGoBuy5rYSxAfV3jaRJWHH6nN9WK4NRExGxsk1bCJ",
        curve_family=CurveFamily.CONSTANT_PRODUCT_VIRTUAL,
        state_account="Pool",
        adapter="vertigo",
        idl_file="vertigo_amm.json",
        docs="https://www.npmjs.com/package/@vertigo-amm/vertigo-sdk",
        notes=(
            "One-sided constant product: `shift` is the virtual quote reserve and "
            "is set to the intended initial market cap in lamports, which makes "
            "the launch market cap readable straight off the pool."
        ),
    ),
    LaunchpadSpec(
        key="gofundmeme",
        display_name="GoFundMeme",
        program_id="GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7",
        curve_family=CurveFamily.POWER_LAW,
        state_account="BondingCurvePool",
        adapter="gofundmeme",
        idl_file="gofundmeme.json",
        graduates_to="orca_raydium_or_meteora",
        docs="https://www.npmjs.com/package/@gofundmeme/sdk",
        notes=(
            "Stores curveConstant and curveExponent as f64 on the pool and "
            "integrates (x+c)^-e over quote raised, so the raise target "
            "(targetRaise) is explicit and the price is analytic."
        ),
    ),
    # ---- programs whose layout is learned from their on-chain IDL --------
    LaunchpadSpec(
        key="boop",
        display_name="Boop.fun",
        program_id="boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4",
        curve_family=CurveFamily.CONSTANT_PRODUCT_VIRTUAL,
        state_account="BondingCurve",
        adapter="heuristic",
        requires_onchain_idl=True,
        graduates_to="raydium",
        docs="https://solscan.io/account/boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4",
        notes=(
            "Publishes its Anchor IDL on chain, so the decoder learns the "
            "BondingCurve layout at runtime. pump.fun-shaped virtual reserves."
        ),
    ),
    LaunchpadSpec(
        key="daosfun",
        display_name="Daos.fun",
        program_id="5jnapfrAN47UYkLkEf7HnprPPBCQLvkYWGZDeKkaP5hv",
        curve_family=CurveFamily.CONSTANT_PRODUCT_VIRTUAL,
        state_account="",
        adapter="heuristic",
        requires_onchain_idl=True,
        docs="https://solscan.io/account/5jnapfrAN47UYkLkEf7HnprPPBCQLvkYWGZDeKkaP5hv",
        notes=(
            "Fund-raise first, then the DAO token trades on a virtual AMM. The "
            "curve account name is discovered from the on-chain IDL."
        ),
    ),
)

BY_KEY: Dict[str, LaunchpadSpec] = {spec.key: spec for spec in LAUNCHPADS}
BY_PROGRAM: Dict[str, LaunchpadSpec] = {spec.program_id: spec for spec in LAUNCHPADS}


def get(key_or_program: str) -> LaunchpadSpec:
    spec = BY_KEY.get(key_or_program) or BY_PROGRAM.get(key_or_program)
    if spec is None:
        raise KeyError(
            f"unknown launchpad {key_or_program!r}; known: {sorted(BY_KEY)}"
        )
    return spec


def program_ids() -> List[str]:
    return [spec.program_id for spec in LAUNCHPADS]


# --------------------------------------------------------------------------
# platform discovery
# --------------------------------------------------------------------------


@dataclass
class PlatformInfo:
    """One tenant of a multi-tenant launch program."""

    launchpad_key: str
    program_id: str
    config_address: str
    config_account_type: str
    name: str = ""
    fields: Dict = field(default_factory=dict)

    def as_dict(self) -> Dict:
        return {
            "launchpad": self.launchpad_key,
            "program_id": self.program_id,
            "config_address": self.config_address,
            "config_account_type": self.config_account_type,
            "name": self.name,
            "fields": self.fields,
        }


def _decode_name(raw) -> str:
    """LaunchLab stores platform names as fixed-size, zero-padded byte arrays."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        try:
            return bytes(int(b) for b in raw).rstrip(b"\x00").decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return ""


#: Platform config addresses verified against a published SDK constant.  This is
#: a convenience shortcut only -- `LaunchpadDecoder.discover_platforms()` reads
#: the authoritative, current list off the chain.
VERIFIED_PLATFORMS: Tuple[PlatformInfo, ...] = (
    PlatformInfo(
        launchpad_key="raydium_launchlab",
        program_id="LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
        config_address="FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1",
        config_account_type="PlatformConfig",
        name="LetsBonk.fun",
        fields={"platform_admin": "2P56vRWDrCBGkqYXxgSWAnuZQZrJPySRQGToTJThpmkN"},
    ),
)


#: fields worth surfacing per config account type when listing platforms
PLATFORM_SUMMARY_FIELDS: Dict[str, Sequence[str]] = {
    "PlatformConfig": ("fee_rate", "creator_fee_rate", "platform_fee_wallet", "cpswap_config"),
    "PoolConfig": (
        "quote_mint",
        "token_decimal",
        "migration_quote_threshold",
        "migration_option",
        "sqrt_start_price",
        "migration_sqrt_price",
        "swap_base_amount",
        "migration_base_threshold",
        "pre_migration_token_supply",
        "post_migration_token_supply",
        "fixed_token_supply_flag",
    ),
    "GlobalConfig": ("curve_type", "index", "trade_fee_rate", "quote_mint", "min_base_supply"),
}
