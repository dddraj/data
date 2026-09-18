"""Account sources.

The decoder never talks to a specific node implementation.  It asks an
`AccountSource` for bytes.  That keeps the same decoding logic usable from a
JSON-RPC endpoint, a Yellowstone/Geyser gRPC stream, an accountsdb snapshot
reader, or a fixture in a unit test -- which is the point of decoding at the
program level.
"""

from __future__ import annotations

import base64
import itertools
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Protocol, Sequence

from .base58 import b58decode


@dataclass(frozen=True)
class AccountInfo:
    pubkey: str
    owner: str
    lamports: int
    data: bytes
    executable: bool = False
    rent_epoch: int = 0
    slot: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


class AccountSource(Protocol):
    """Everything the decoder needs from a node."""

    def get_account(self, pubkey: str) -> Optional[AccountInfo]: ...

    def get_multiple_accounts(
        self, pubkeys: Sequence[str]
    ) -> List[Optional[AccountInfo]]: ...

    def get_program_accounts(
        self,
        program_id: str,
        *,
        data_size: Optional[int] = None,
        memcmp: Optional[Sequence[tuple]] = None,
        limit: Optional[int] = None,
    ) -> List[AccountInfo]: ...

    def get_slot(self) -> int: ...


class RpcError(RuntimeError):
    pass


class JsonRpcAccountSource:
    """Plain JSON-RPC client over urllib -- works against any Solana node."""

    def __init__(
        self,
        endpoint: str,
        *,
        commitment: str = "confirmed",
        timeout: float = 30.0,
        max_retries: int = 4,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.endpoint = endpoint
        self.commitment = commitment
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._ids = itertools.count(1)

    # -- transport ------------------------------------------------------
    def _call(self, method: str, params: list):
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        ).encode()
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(self.endpoint, data=payload, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode())
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt == self.max_retries - 1:
                    raise RpcError(f"{method} failed after {self.max_retries} tries: {exc}") from exc
                time.sleep(2**attempt)
        else:  # pragma: no cover - loop always breaks or raises
            raise RpcError(str(last_exc))

        if "error" in body:
            raise RpcError(f"{method}: {body['error']}")
        return body["result"]

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _to_account(pubkey: str, value: Optional[dict], slot: int) -> Optional[AccountInfo]:
        if not value:
            return None
        data_field = value["data"]
        if isinstance(data_field, list):
            raw = base64.b64decode(data_field[0])
        else:  # jsonParsed fallback -- no bytes available
            raw = b""
        return AccountInfo(
            pubkey=pubkey,
            owner=value["owner"],
            lamports=value["lamports"],
            data=raw,
            executable=value.get("executable", False),
            rent_epoch=value.get("rentEpoch", 0),
            slot=slot,
        )

    # -- AccountSource --------------------------------------------------
    def get_account(self, pubkey: str) -> Optional[AccountInfo]:
        result = self._call(
            "getAccountInfo",
            [pubkey, {"encoding": "base64", "commitment": self.commitment}],
        )
        slot = (result or {}).get("context", {}).get("slot", 0)
        return self._to_account(pubkey, (result or {}).get("value"), slot)

    def get_multiple_accounts(self, pubkeys: Sequence[str]) -> List[Optional[AccountInfo]]:
        out: List[Optional[AccountInfo]] = []
        for start in range(0, len(pubkeys), 100):  # RPC caps the batch at 100
            chunk = list(pubkeys[start : start + 100])
            result = self._call(
                "getMultipleAccounts",
                [chunk, {"encoding": "base64", "commitment": self.commitment}],
            )
            slot = result.get("context", {}).get("slot", 0)
            for key, value in zip(chunk, result["value"]):
                out.append(self._to_account(key, value, slot))
        return out

    def get_program_accounts(
        self,
        program_id: str,
        *,
        data_size: Optional[int] = None,
        memcmp: Optional[Sequence[tuple]] = None,
        limit: Optional[int] = None,
    ) -> List[AccountInfo]:
        filters: List[dict] = []
        if data_size is not None:
            filters.append({"dataSize": data_size})
        for offset, blob in memcmp or ():
            filters.append({"memcmp": {"offset": offset, "bytes": blob}})
        config = {"encoding": "base64", "commitment": self.commitment, "withContext": True}
        if filters:
            config["filters"] = filters
        result = self._call("getProgramAccounts", [program_id, config])
        rows = result["value"] if isinstance(result, dict) else result
        slot = result.get("context", {}).get("slot", 0) if isinstance(result, dict) else 0
        accounts = [self._to_account(row["pubkey"], row["account"], slot) for row in rows]
        accounts = [a for a in accounts if a is not None]
        return accounts[:limit] if limit else accounts

    def get_slot(self) -> int:
        return self._call("getSlot", [{"commitment": self.commitment}])

    # -- extras used by the mint/supply resolver -------------------------
    def get_token_supply(self, mint: str) -> Optional[dict]:
        result = self._call("getTokenSupply", [mint, {"commitment": self.commitment}])
        return (result or {}).get("value")

    def get_token_account_balance(self, token_account: str) -> Optional[dict]:
        result = self._call(
            "getTokenAccountBalance", [token_account, {"commitment": self.commitment}]
        )
        return (result or {}).get("value")


class StaticAccountSource:
    """In-memory source, for tests and for replaying captured account bytes."""

    def __init__(self, accounts: Optional[Dict[str, AccountInfo]] = None, slot: int = 0) -> None:
        self._accounts: Dict[str, AccountInfo] = dict(accounts or {})
        self._slot = slot

    def add(self, account: AccountInfo) -> None:
        self._accounts[account.pubkey] = account

    def add_raw(
        self,
        pubkey: str,
        owner: str,
        data: bytes,
        lamports: int = 0,
        executable: bool = False,
    ) -> None:
        self.add(
            AccountInfo(
                pubkey=pubkey,
                owner=owner,
                lamports=lamports,
                data=data,
                executable=executable,
                slot=self._slot,
            )
        )

    def get_account(self, pubkey: str) -> Optional[AccountInfo]:
        return self._accounts.get(pubkey)

    def get_multiple_accounts(self, pubkeys: Sequence[str]) -> List[Optional[AccountInfo]]:
        return [self._accounts.get(key) for key in pubkeys]

    def get_program_accounts(
        self,
        program_id: str,
        *,
        data_size: Optional[int] = None,
        memcmp: Optional[Sequence[tuple]] = None,
        limit: Optional[int] = None,
    ) -> List[AccountInfo]:
        rows: Iterable[AccountInfo] = (
            a for a in self._accounts.values() if a.owner == program_id
        )
        if data_size is not None:
            rows = (a for a in rows if a.size == data_size)
        for offset, blob in memcmp or ():
            expected = b58decode(blob)
            rows = [
                a
                for a in rows
                if a.data[offset : offset + len(expected)] == expected
            ]
        out = list(rows)
        return out[:limit] if limit else out

    def get_slot(self) -> int:
        return self._slot
