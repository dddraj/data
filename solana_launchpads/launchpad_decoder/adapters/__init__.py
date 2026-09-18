"""Adapter registry."""

from __future__ import annotations

from typing import Dict

from .base import Adapter, ConfigStore, DecodeContext
from .gofundmeme import GoFundMemeAdapter
from .heaven import HeavenAdapter
from .heuristic import HeuristicAdapter, map_fields
from .meteora_dbc import MeteoraDbcAdapter
from .moonit import MoonitAdapter
from .pumpfun import PumpFunAdapter
from .pumpswap import PumpSwapAdapter
from .raydium_launchlab import RaydiumLaunchLabAdapter
from .vertigo import VertigoAdapter

ADAPTERS: Dict[str, Adapter] = {
    "pumpfun": PumpFunAdapter(),
    "pumpswap": PumpSwapAdapter(),
    "raydium_launchlab": RaydiumLaunchLabAdapter(),
    "meteora_dbc": MeteoraDbcAdapter(),
    "moonit": MoonitAdapter(),
    "heaven": HeavenAdapter(),
    "vertigo": VertigoAdapter(),
    "gofundmeme": GoFundMemeAdapter(),
    "heuristic": HeuristicAdapter(),
}


def get_adapter(name: str) -> Adapter:
    adapter = ADAPTERS.get(name)
    if adapter is None:
        return ADAPTERS["heuristic"]
    return adapter


__all__ = [
    "ADAPTERS",
    "Adapter",
    "ConfigStore",
    "DecodeContext",
    "get_adapter",
    "map_fields",
]
