"""Adapter plumbing shared by every launchpad."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ..anchor_idl import ProgramSchema
from ..registry import LaunchpadSpec
from ..rpc import AccountSource
from ..types import CurveFamily, LaunchMetrics, Param, ValueSource

#: decimals for the quote mints launchpads actually use, so the common case
#: needs no extra RPC round trip
KNOWN_MINT_DECIMALS: Dict[str, int] = {
    "So11111111111111111111111111111111111111112": 9,  # wSOL
    "11111111111111111111111111111111": 9,  # native SOL sentinel
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,  # USDT
}


class ConfigStore:
    """Caches decoded program config accounts and drops them on an upgrade.

    Launch parameters -- initial virtual reserves, supply, migration threshold,
    fee rates -- live in these accounts.  They are "static" only in the sense
    that they rarely change; they are program state, so they are read from the
    chain and re-read whenever the program is redeployed.
    """

    def __init__(self, source: Optional[AccountSource]) -> None:
        self.source = source
        self._cache: Dict[Tuple[str, str], Tuple[Optional[str], Optional[dict], int]] = {}

    def invalidate(self, program_id: Optional[str] = None) -> None:
        if program_id is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k[0] == program_id]:
            self._cache.pop(key, None)

    def preload(self, program_id: str, address: str, name: str, decoded: dict, slot: int = 0) -> None:
        """Seed the cache directly -- used by tests and offline replay."""
        self._cache[(program_id, address)] = (name, decoded, slot)

    def fetch(
        self, program_id: str, address: str, schema: Optional[ProgramSchema]
    ) -> Tuple[Optional[str], Optional[dict], int]:
        key = (program_id, address)
        if key in self._cache:
            return self._cache[key]
        result: Tuple[Optional[str], Optional[dict], int] = (None, None, 0)
        if self.source is not None and schema is not None:
            account = self.source.get_account(address)
            if account is not None and account.data:
                decoded = schema.decode_account(account.data)
                if decoded is not None:
                    result = (decoded[0], decoded[1], account.slot)
        self._cache[key] = result
        return result


@dataclass
class DecodeContext:
    """Everything an adapter may consult while decoding one account."""

    spec: LaunchpadSpec
    schema: ProgramSchema
    source: Optional[AccountSource] = None
    configs: ConfigStore = field(default_factory=lambda: ConfigStore(None))
    slot: int = 0
    program_revision: Optional[tuple] = None
    #: extra mint -> decimals overrides supplied by the caller
    mint_decimals: Dict[str, int] = field(default_factory=dict)
    #: when True, unknown mint decimals are looked up over RPC
    resolve_mints: bool = True

    def decimals_for(self, mint: Optional[str]) -> Optional[int]:
        if not mint:
            return None
        if mint in self.mint_decimals:
            return self.mint_decimals[mint]
        if mint in KNOWN_MINT_DECIMALS:
            return KNOWN_MINT_DECIMALS[mint]
        if self.resolve_mints and self.source is not None:
            getter = getattr(self.source, "get_token_supply", None)
            if callable(getter):
                try:
                    value = getter(mint)
                except Exception:  # noqa: BLE001 - decimals are best-effort
                    value = None
                if value and "decimals" in value:
                    decimals = int(value["decimals"])
                    self.mint_decimals[mint] = decimals
                    return decimals
            account = self.source.get_account(mint)
            if account is not None and len(account.data) >= 45:
                # SPL mint layout: supply at 36..44, decimals at 44
                decimals = account.data[44]
                self.mint_decimals[mint] = decimals
                return decimals
        return None

    def config_source(self) -> ValueSource:
        return (
            ValueSource.ONCHAIN_CONFIG if self.source is not None else ValueSource.BUNDLED_SNAPSHOT
        )


class Adapter:
    """Turn one decoded curve account into `LaunchMetrics`."""

    key: str = ""
    curve_family: CurveFamily = CurveFamily.UNKNOWN

    def state_account_names(self, ctx: DecodeContext) -> Tuple[str, ...]:
        return (ctx.spec.state_account,) if ctx.spec.state_account else ()

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:  # pragma: no cover - interface
        raise NotImplementedError

    # -- helpers available to every adapter ------------------------------
    @staticmethod
    def new_metrics(
        ctx: DecodeContext, account_name: str, address: Optional[str], state: Dict[str, Any]
    ) -> LaunchMetrics:
        return LaunchMetrics(
            launchpad=ctx.spec.key,
            program_id=ctx.spec.program_id,
            curve_address=address,
            curve_account_type=account_name,
            curve_family=ctx.spec.curve_family,
            slot=ctx.slot,
            schema_source=ctx.schema.source,
            schema_fingerprint=ctx.schema.fingerprint,
            program_revision=ctx.program_revision,
            raw_state=state,
        )

    @staticmethod
    def param(value: Any, source: ValueSource, slot: int = 0, note: str = "") -> Param:
        return Param(value=value, source=source, slot=slot, note=note)

    @staticmethod
    def finish(metrics: LaunchMetrics) -> LaunchMetrics:
        """Fill in market caps and progress once prices and supply are known."""
        supply = metrics.total_supply.value
        for price_name, mcap_name in (
            ("launch_price_quote", "launch_mcap_quote"),
            ("current_price_quote", "current_mcap_quote"),
            ("graduation_price_quote", "graduation_mcap_quote"),
        ):
            mcap = getattr(metrics, mcap_name)
            if mcap.value is not None:
                continue
            price = getattr(metrics, price_name).value
            if price is None or supply is None:
                continue
            setattr(
                metrics,
                mcap_name,
                Param(
                    value=price * supply,
                    source=ValueSource.DERIVED,
                    slot=metrics.slot,
                    note=f"{price_name} x total_supply",
                ),
            )

        if metrics.progress.value is None:
            raised = metrics.raised_quote.value
            target = metrics.raise_target_quote.value
            if raised is not None and target:
                metrics.progress = Param(
                    value=max(0.0, min(1.0, raised / target)),
                    source=ValueSource.DERIVED,
                    slot=metrics.slot,
                    note="raised / raise_target",
                )
        return metrics
