"""Program-level decoder for Solana bonding-curve launchpads.

    from launchpad_decoder import LaunchpadDecoder, JsonRpcAccountSource

    source  = JsonRpcAccountSource("https://your-node:8899")
    decoder = LaunchpadDecoder(source)

    metrics = decoder.decode_address("<bonding curve / pool address>")
    print(metrics.launch_price_quote.value, metrics.raise_target_quote.value)

    # on a timer, or on a ProgramData account update from your Geyser plugin:
    report = decoder.refresh()
    if report:
        print(report.describe())
"""

from .anchor_idl import ProgramSchema, account_discriminator, compile_idl
from .base58 import b58decode, b58encode
from .curves import LiquiditySegment
from .decoder import LaunchpadDecoder, ParamChange, RefreshReport
from .program_state import (
    ProgramDeployment,
    ProgramUpgrade,
    ProgramWatcher,
    fetch_onchain_idl,
    idl_address,
    programdata_address,
)
from .registry import LAUNCHPADS, LaunchpadSpec, PlatformInfo
from .rpc import AccountInfo, JsonRpcAccountSource, StaticAccountSource
from .types import CurveFamily, LaunchMetrics, Param, ValueSource

__all__ = [
    "AccountInfo",
    "CurveFamily",
    "JsonRpcAccountSource",
    "LAUNCHPADS",
    "LaunchMetrics",
    "LaunchpadDecoder",
    "LaunchpadSpec",
    "LiquiditySegment",
    "Param",
    "ParamChange",
    "PlatformInfo",
    "ProgramDeployment",
    "ProgramSchema",
    "ProgramUpgrade",
    "ProgramWatcher",
    "RefreshReport",
    "StaticAccountSource",
    "ValueSource",
    "account_discriminator",
    "b58decode",
    "b58encode",
    "compile_idl",
    "fetch_onchain_idl",
    "idl_address",
    "programdata_address",
]

__version__ = "0.1.0"
