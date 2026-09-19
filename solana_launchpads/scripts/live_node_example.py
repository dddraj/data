#!/usr/bin/env python3
"""Wiring the decoder into a live node feed.

The decoder never fetches on its own during the hot path: you hand it bytes,
it hands you metrics.  That makes it drop-in for whatever account stream your
node already produces -- Yellowstone/Geyser gRPC, a Geyser plugin, a
`accountSubscribe` websocket, or a ledger replay.

Three things have to be wired up:

1. **The account filter.**  Subscribe to accounts owned by the launchpad
   program ids, optionally narrowed to the curve account's 8-byte
   discriminator (`curve_account_filters()` below emits both).
2. **The decode call.**  `decode_account_data(owner, data, address, slot)`.
   Returns `None` for accounts that are not curve state, so it is safe to feed
   it every update from the program.
3. **The upgrade watch.**  Also subscribe to each program's ProgramData
   account.  When one of those updates, call `decoder.refresh()`; cached IDLs
   and config accounts are dropped and the parameter diff is returned so you
   can log exactly which launch constant moved.

**And the part that is easy to miss.**  Hot-swapping a layout is only half of
a hot reload.  Most streams compose their subscription once, at connect time
-- a Geyser `SubscribeRequest`, a websocket `accountSubscribe` -- so an account
whose owner is not already in that request never arrives, however good the
decoder is.  Two things change the filter set under you: a program upgrade that
renames an account struct (its 8-byte discriminator moves, so the old memcmp
matches nothing), and a newly learned program (its owner was never subscribed
at all).  `LiveDecoder` therefore owns its filter set and reports a
`SubscriptionChange` whenever it moves; act on that by re-issuing the
subscription, or the swapped layout decodes a stream that has gone quiet.

Run this file directly for a simulated feed that needs no node:

    python scripts/live_node_example.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launchpad_decoder import (  # noqa: E402
    LaunchMetrics,
    LaunchpadDecoder,
    StaticAccountSource,
)
from launchpad_decoder.base58 import b58encode  # noqa: E402


def curve_account_filters(decoder: LaunchpadDecoder) -> List[Dict[str, object]]:
    """Subscription filters: one per (program, curve account type).

    `memcmp_base58` is the account discriminator, so the node only sends you
    curve state rather than every account the program owns.
    """
    filters: List[Dict[str, object]] = []
    for program_id, spec in decoder.specs.items():
        schema = decoder.schema(program_id)
        if schema is None:
            filters.append({"launchpad": spec.key, "owner": program_id, "memcmp_base58": None})
            continue
        # Prefer the registered state account, but only while the schema still
        # has it. A redeploy that RENAMES the struct would otherwise leave this
        # program with no filter at all: the old name resolves to nothing and
        # the new one is never reached, so the stream goes quiet with no error
        # anywhere. Falling back to whatever the schema now declares keeps the
        # subscription alive across the rename.
        if spec.state_account and spec.state_account in schema.accounts:
            names = [spec.state_account]
        else:
            names = list(schema.accounts)
        for name in names:
            account = schema.accounts.get(name)
            if account is None:
                continue
            filters.append(
                {
                    "launchpad": spec.key,
                    "owner": program_id,
                    "account_type": name,
                    "memcmp_offset": 0,
                    "memcmp_base58": b58encode(account.discriminator),
                }
            )
    return filters


def programdata_filters(decoder: LaunchpadDecoder) -> Dict[str, str]:
    """ProgramData addresses to subscribe to, keyed by program id.

    An update on any of these means the launchpad redeployed.
    """
    return decoder.watcher.programdata_addresses()


def filter_key(entry: Dict[str, object]) -> Tuple:
    """Identity of one filter, for diffing two subscription sets."""
    return (entry.get("owner"), entry.get("account_type"), entry.get("memcmp_base58"))


@dataclass
class SubscriptionChange:
    """The filter set moved -- re-issue the subscription.

    A decoder that hot-swaps its layouts while the stream keeps delivering the
    old filter set has fixed nothing: accounts it can now decode never arrive.
    """

    reason: str
    added: List[Dict[str, object]] = field(default_factory=list)
    removed: List[Dict[str, object]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed)

    def describe(self) -> str:
        parts = []
        for entry in self.added:
            parts.append(f"+{entry.get('launchpad')}/{entry.get('account_type') or '*'}")
        for entry in self.removed:
            parts.append(f"-{entry.get('launchpad')}/{entry.get('account_type') or '*'}")
        return f"{self.reason}: " + ", ".join(parts)


class LiveDecoder:
    """A thin event loop you can drop your stream into."""

    def __init__(self, decoder: LaunchpadDecoder) -> None:
        self.decoder = decoder
        self.programdata = {
            address: program_id
            for program_id, address in programdata_filters(decoder).items()
        }
        if decoder.source is not None:
            decoder.snapshot_static_params()
        self._filters: List[Dict[str, object]] = curve_account_filters(decoder)
        #: set when the filter set moves; clear it once you have resubscribed
        self.pending_resubscribe: Optional[SubscriptionChange] = None

    def filters(self) -> List[Dict[str, object]]:
        """The subscription the node should currently be serving."""
        return list(self._filters)

    def _sync_filters(self, reason: str) -> Optional[SubscriptionChange]:
        """Recompute the filter set and report any movement."""
        current = curve_account_filters(self.decoder)
        before = {filter_key(entry): entry for entry in self._filters}
        after = {filter_key(entry): entry for entry in current}
        change = SubscriptionChange(
            reason=reason,
            added=[entry for key, entry in after.items() if key not in before],
            removed=[entry for key, entry in before.items() if key not in after],
        )
        self._filters = current
        if not change:
            return None
        self.pending_resubscribe = change
        return change

    def on_account_update(
        self, owner: str, address: str, data: bytes, slot: int
    ) -> Optional[LaunchMetrics]:
        """Feed every account update here. Returns metrics, or None."""
        if address in self.programdata:
            self.on_program_upgrade(self.programdata[address])
            return None
        known = owner in self.decoder.specs
        metrics = self.decoder.decode_account_data(owner, data, address, slot)
        if not known and owner in self.decoder.specs:
            # decode_account_data adopted a program nobody had catalogued. Its
            # accounts are reaching us only because this one happened to be in
            # the stream already; the rest need a subscription.
            change = self._sync_filters(f"learned program {owner}")
            if change:
                print(f"[subscribe] {change.describe()}")
        return metrics

    def on_program_upgrade(self, program_id: str) -> None:
        report = self.decoder.refresh()
        if not report:
            return
        for upgrade in report.upgrades:
            print(f"[upgrade] {upgrade.describe()}")
        for change in report.changes:
            print(f"[param]   {change.describe()}")
        # A redeploy can rename an account struct, which moves its
        # discriminator. The old memcmp then matches nothing and the stream
        # goes quiet -- silently, which is the worst way for it to fail.
        moved = self._sync_filters(f"upgrade of {program_id}")
        if moved:
            print(f"[subscribe] {moved.describe()}")


# --------------------------------------------------------------------------
# a runnable simulation, so the wiring can be checked without a node
# --------------------------------------------------------------------------


def _simulated_feed() -> Iterable[Tuple[str, str, bytes, int]]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from fixtures import encode_account  # type: ignore

    from launchpad_decoder.anchor_idl import compile_idl
    from launchpad_decoder.registry import get as get_spec

    spec = get_spec("pumpfun")
    idl = json.loads(
        (Path(__file__).resolve().parents[1] / "launchpad_decoder/idl/pump.json").read_text()
    )
    schema = compile_idl(idl)
    ivt, ivs = 1_073_000_000_000_000, 30_000_000_000
    for step, paid in enumerate([0, 10_000_000_000, 42_500_000_000, 85_005_359_057]):
        v_quote = ivs + paid
        v_base = (ivt * ivs) // v_quote
        sold = ivt - v_base
        blob = encode_account(
            idl,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": v_base,
                "virtual_quote_reserves": v_quote,
                "real_token_reserves": max(0, 793_100_000_000_000 - sold),
                "real_quote_reserves": paid,
                "token_total_supply": 1_000_000_000_000_000,
                "complete": paid >= 85_005_359_057,
            },
        )
        yield spec.program_id, "So11111111111111111111111111111111111111112", blob, 1000 + step


def main() -> None:
    decoder = LaunchpadDecoder(StaticAccountSource(), resolve_mints=False)
    live = LiveDecoder(decoder)

    print("subscription filters your node needs:")
    for entry in curve_account_filters(decoder)[:4]:
        print("   ", json.dumps(entry))
    print(f"    ... {len(curve_account_filters(decoder))} filters in total")
    print("\nProgramData accounts to watch for redeploys:")
    for program_id, address in list(programdata_filters(decoder).items())[:3]:
        print(f"    {program_id} -> {address}")

    print("\nsimulated pump.fun curve updates:")
    print(f"  {'slot':>6} {'price (SOL)':>16} {'mcap (SOL)':>12} {'raised':>10} {'progress':>9}")
    for owner, address, data, slot in _simulated_feed():
        metrics = live.on_account_update(owner, address, data, slot)
        if metrics is None:
            continue
        print(
            f"  {slot:>6} {metrics.current_price_quote.value:>16.10f} "
            f"{metrics.current_mcap_quote.value:>12.2f} "
            f"{metrics.raised_quote.value:>10.3f} "
            f"{metrics.progress.value:>9.1%}"
        )


if __name__ == "__main__":
    main()
