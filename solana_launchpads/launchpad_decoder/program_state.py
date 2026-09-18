"""Program-deployment introspection: upgrades, and the on-chain Anchor IDL.

Two facilities live here, and together they are what lets a running decoder
survive a launchpad shipping a new program build:

``ProgramWatcher``
    Reads the BPF upgradeable loader's ``ProgramData`` account for each
    program and reports the deployment slot, upgrade authority and (optionally)
    a hash of the executable.  Any change means the program was redeployed and
    every value cached from it must be re-derived.

``fetch_onchain_idl``
    Reads the program's IDL account (``create_with_seed(pda, "anchor:idl", program)``)
    and inflates it.  When a launchpad publishes its IDL on chain -- Boop and
    several smaller pads do -- the decoder can learn a brand-new account layout
    at runtime without a code change.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .anchor_idl import ProgramSchema, compile_idl
from .base58 import b58encode
from .pubkey import create_with_seed, find_program_address
from .rpc import AccountInfo, AccountSource

BPF_LOADER_UPGRADEABLE = "BPFLoaderUpgradeab1e11111111111111111111111"
BPF_LOADER_2 = "BPFLoader2111111111111111111111111111111111"
BPF_LOADER_DEPRECATED = "BPFLoader1111111111111111111111111111111111"
LOADER_V4 = "LoaderV411111111111111111111111111111111111"

#: `UpgradeableLoaderState::size_of_programdata_metadata()`
PROGRAMDATA_HEADER_LEN = 45

_STATE_UNINITIALIZED = 0
_STATE_BUFFER = 1
_STATE_PROGRAM = 2
_STATE_PROGRAMDATA = 3

IDL_SEED = "anchor:idl"


class ProgramStateError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# upgradeable loader
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgramDeployment:
    """A snapshot of how a program is currently deployed."""

    program_id: str
    loader: str
    upgradeable: bool
    programdata_address: Optional[str] = None
    last_deploy_slot: Optional[int] = None
    upgrade_authority: Optional[str] = None
    executable_len: Optional[int] = None
    executable_hash: Optional[str] = None
    observed_slot: int = 0

    @property
    def immutable(self) -> bool:
        """Upgradeable programs whose authority was revoked can never change."""
        return not self.upgradeable or self.upgrade_authority is None

    def revision(self) -> Tuple:
        """The tuple that changes exactly when the deployed code changes."""
        return (self.loader, self.last_deploy_slot, self.executable_hash, self.executable_len)


def programdata_address(program_id: str) -> str:
    from .pubkey import seeds_to_bytes

    address, _ = find_program_address(seeds_to_bytes([program_id]), BPF_LOADER_UPGRADEABLE)
    return address


def parse_program_account(data: bytes) -> Optional[str]:
    """`UpgradeableLoaderState::Program` -> the ProgramData address."""
    if len(data) < 36:
        return None
    if int.from_bytes(data[:4], "little") != _STATE_PROGRAM:
        return None
    return b58encode(data[4:36])


def parse_programdata_account(data: bytes) -> Optional[Tuple[int, Optional[str]]]:
    """`UpgradeableLoaderState::ProgramData` -> (deploy slot, upgrade authority)."""
    if len(data) < PROGRAMDATA_HEADER_LEN:
        return None
    if int.from_bytes(data[:4], "little") != _STATE_PROGRAMDATA:
        return None
    slot = int.from_bytes(data[4:12], "little")
    has_authority = data[12]
    authority = b58encode(data[13:45]) if has_authority else None
    return slot, authority


def read_deployment(
    source: AccountSource,
    program_id: str,
    *,
    hash_executable: bool = False,
) -> ProgramDeployment:
    """Describe how `program_id` is deployed right now."""
    program_account = source.get_account(program_id)
    if program_account is None:
        raise ProgramStateError(f"program {program_id} not found on this cluster")

    loader = program_account.owner
    if loader != BPF_LOADER_UPGRADEABLE:
        # BPFLoader2 / deprecated loader programs cannot be upgraded at all.
        return ProgramDeployment(
            program_id=program_id,
            loader=loader,
            upgradeable=False,
            executable_len=program_account.size,
            executable_hash=(
                hashlib.sha256(program_account.data).hexdigest()
                if hash_executable and program_account.data
                else None
            ),
            observed_slot=program_account.slot,
        )

    pd_address = parse_program_account(program_account.data) or programdata_address(program_id)
    pd_account = source.get_account(pd_address)
    if pd_account is None:
        raise ProgramStateError(f"ProgramData {pd_address} for {program_id} not found")

    parsed = parse_programdata_account(pd_account.data)
    if parsed is None:
        raise ProgramStateError(f"account {pd_address} is not a ProgramData account")
    slot, authority = parsed

    elf = pd_account.data[PROGRAMDATA_HEADER_LEN:]
    return ProgramDeployment(
        program_id=program_id,
        loader=loader,
        upgradeable=True,
        programdata_address=pd_address,
        last_deploy_slot=slot,
        upgrade_authority=authority,
        executable_len=len(elf),
        executable_hash=hashlib.sha256(elf).hexdigest() if hash_executable else None,
        observed_slot=pd_account.slot,
    )


@dataclass
class ProgramUpgrade:
    program_id: str
    previous: ProgramDeployment
    current: ProgramDeployment

    def describe(self) -> str:
        return (
            f"{self.program_id} redeployed: slot "
            f"{self.previous.last_deploy_slot} -> {self.current.last_deploy_slot}"
        )


class ProgramWatcher:
    """Tracks deployments and reports which programs were redeployed.

    Wire `poll()` into whatever loop the node decoder already runs (a timer, a
    slot subscription, or a Geyser account-update filter on the ProgramData
    addresses returned by `programdata_addresses()`).
    """

    def __init__(
        self,
        source: AccountSource,
        program_ids: Sequence[str] = (),
        *,
        hash_executable: bool = False,
    ) -> None:
        self.source = source
        self.hash_executable = hash_executable
        self._known: Dict[str, ProgramDeployment] = {}
        self._missing: Dict[str, str] = {}
        self.program_ids: List[str] = list(dict.fromkeys(program_ids))

    def track(self, program_id: str) -> None:
        if program_id not in self.program_ids:
            self.program_ids.append(program_id)

    def deployment(self, program_id: str) -> Optional[ProgramDeployment]:
        return self._known.get(program_id)

    def programdata_addresses(self) -> Dict[str, str]:
        """ProgramData address per tracked program -- subscribe to these."""
        out = {}
        for pid in self.program_ids:
            deployment = self._known.get(pid)
            address = deployment.programdata_address if deployment else None
            out[pid] = address or programdata_address(pid)
        return out

    def poll(self, program_ids: Optional[Sequence[str]] = None) -> List[ProgramUpgrade]:
        upgrades: List[ProgramUpgrade] = []
        for pid in program_ids or self.program_ids:
            try:
                current = read_deployment(
                    self.source, pid, hash_executable=self.hash_executable
                )
            except Exception as exc:  # noqa: BLE001 - one bad program must not stop the rest
                self._missing[pid] = str(exc)
                continue
            self._missing.pop(pid, None)
            previous = self._known.get(pid)
            self._known[pid] = current
            if previous is not None and previous.revision() != current.revision():
                upgrades.append(ProgramUpgrade(pid, previous, current))
        return upgrades

    @property
    def unreachable(self) -> Dict[str, str]:
        return dict(self._missing)


# --------------------------------------------------------------------------
# on-chain IDL
# --------------------------------------------------------------------------


def idl_address(program_id: str) -> str:
    """Anchor's canonical IDL account address for a program."""
    base, _ = find_program_address([], program_id)
    return create_with_seed(base, IDL_SEED, program_id)


@dataclass
class OnchainIdl:
    program_id: str
    address: str
    authority: Optional[str]
    document: dict
    raw_len: int
    slot: int = 0

    def compile(self) -> ProgramSchema:
        schema = compile_idl(self.document, source=f"onchain:{self.address}")
        if schema.program_id is None:
            schema.program_id = self.program_id
        return schema


def _inflate(blob: bytes) -> bytes:
    for decompressor in (
        lambda b: zlib.decompress(b),
        lambda b: zlib.decompressobj().decompress(b),
        lambda b: zlib.decompress(b, -zlib.MAX_WBITS),
        lambda b: zlib.decompress(b, 16 + zlib.MAX_WBITS),
    ):
        try:
            out = decompressor(blob)
            if out:
                return out
        except zlib.error:
            continue
    return blob  # some programs store the JSON uncompressed


def parse_idl_account(data: bytes) -> Tuple[Optional[str], dict, int]:
    """Decode an Anchor `IdlAccount`: disc(8) + authority(32) + len(u32) + zlib(json)."""
    if len(data) < 44:
        raise ProgramStateError("IDL account is too small to be an Anchor IdlAccount")
    authority = b58encode(data[8:40])
    declared_len = int.from_bytes(data[40:44], "little")
    blob = data[44 : 44 + declared_len] if declared_len else data[44:]
    text = _inflate(blob)
    try:
        document = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProgramStateError(
            "IDL payload is not JSON after inflation -- this program predates "
            "Anchor's JSON IDL storage and would need a borsh IDL decoder"
        ) from exc
    return authority, document, declared_len


def fetch_onchain_idl(source: AccountSource, program_id: str) -> Optional[OnchainIdl]:
    """Fetch and inflate a program's on-chain IDL, or ``None`` if it has none."""
    address = idl_address(program_id)
    account = source.get_account(address)
    if account is None or not account.data:
        return None
    authority, document, raw_len = parse_idl_account(account.data)
    return OnchainIdl(
        program_id=program_id,
        address=address,
        authority=authority,
        document=document,
        raw_len=raw_len,
        slot=account.slot,
    )


def account_data_fingerprint(account: Optional[AccountInfo]) -> str:
    if account is None:
        return ""
    return hashlib.sha256(account.data).hexdigest()[:16]


@dataclass
class SchemaCache:
    """Per-program schema cache that is invalidated by a program upgrade."""

    source: AccountSource
    watcher: ProgramWatcher
    prefer_onchain: bool = True
    _schemas: Dict[str, ProgramSchema] = field(default_factory=dict)
    _revisions: Dict[str, Tuple] = field(default_factory=dict)
    _errors: Dict[str, str] = field(default_factory=dict)

    def invalidate(self, program_id: Optional[str] = None) -> None:
        if program_id is None:
            self._schemas.clear()
            self._revisions.clear()
        else:
            self._schemas.pop(program_id, None)
            self._revisions.pop(program_id, None)

    def get(self, program_id: str, fallback: Optional[ProgramSchema]) -> Optional[ProgramSchema]:
        deployment = self.watcher.deployment(program_id)
        revision = deployment.revision() if deployment else None
        if program_id in self._schemas and self._revisions.get(program_id) == revision:
            return self._schemas[program_id]

        schema: Optional[ProgramSchema] = None
        if self.prefer_onchain:
            try:
                onchain = fetch_onchain_idl(self.source, program_id)
                if onchain is not None:
                    schema = onchain.compile()
            except Exception as exc:  # noqa: BLE001 - never let this break decoding
                self._errors[program_id] = f"on-chain IDL unusable: {exc}"
        if schema is None:
            schema = fallback
        if schema is not None:
            self._schemas[program_id] = schema
            self._revisions[program_id] = revision
        return schema

    @property
    def errors(self) -> Dict[str, str]:
        return dict(self._errors)


def summarize_deployment(deployment: ProgramDeployment) -> Dict[str, Any]:
    return {
        "program_id": deployment.program_id,
        "loader": deployment.loader,
        "upgradeable": deployment.upgradeable,
        "immutable": deployment.immutable,
        "programdata_address": deployment.programdata_address,
        "last_deploy_slot": deployment.last_deploy_slot,
        "upgrade_authority": deployment.upgrade_authority,
        "executable_len": deployment.executable_len,
        "executable_hash": deployment.executable_hash,
    }
