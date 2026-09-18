"""The façade: point it at a node, hand it account bytes, get launch metrics.

Design goals, in order:

1. **Program level.** Classification is by ``(owner program, 8-byte account
   discriminator)``.  No mint lists, no platform allowlists, no indexer.
2. **Self-describing.** Layouts come from an Anchor IDL; preferring the one the
   program publishes on chain means a layout change ships itself.
3. **Self-healing.** `refresh()` re-reads each program's ProgramData account.
   If the deployment slot or executable hash moved, every cached schema and
   every cached config account for that program is dropped and re-resolved, and
   the parameter diff is reported so a caller can log or alert on it.
4. **Transport agnostic.** Everything flows through the `AccountSource`
   protocol, so the same code runs over JSON-RPC, a Geyser stream, or a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .adapters import ConfigStore, DecodeContext, get_adapter
from .anchor_idl import ProgramSchema
from .program_state import (
    ProgramDeployment,
    ProgramUpgrade,
    ProgramWatcher,
    SchemaCache,
    fetch_onchain_idl,
    summarize_deployment,
)
from .registry import (
    LAUNCHPADS,
    PLATFORM_SUMMARY_FIELDS,
    LaunchpadSpec,
    PlatformInfo,
    _decode_name,
    get as get_spec,
)
from .rpc import AccountInfo, AccountSource
from .types import CurveFamily, LaunchMetrics


@dataclass
class ParamChange:
    launchpad: str
    path: str
    before: Any
    after: Any

    def describe(self) -> str:
        return f"{self.launchpad}.{self.path}: {self.before!r} -> {self.after!r}"


@dataclass
class RefreshReport:
    upgrades: List[ProgramUpgrade] = field(default_factory=list)
    changes: List[ParamChange] = field(default_factory=list)
    unreachable: Dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.upgrades or self.changes)

    def describe(self) -> str:
        lines = [u.describe() for u in self.upgrades]
        lines += [c.describe() for c in self.changes]
        return "\n".join(lines) or "no program or parameter changes"


class LaunchpadDecoder:
    def __init__(
        self,
        source: Optional[AccountSource] = None,
        *,
        launchpads: Sequence[LaunchpadSpec] = LAUNCHPADS,
        prefer_onchain_idl: bool = True,
        hash_executables: bool = False,
        resolve_mints: bool = True,
        auto_learn: bool = True,
    ) -> None:
        self.source = source
        #: adopt unregistered programs that publish an on-chain IDL, so a
        #: launchpad nobody has catalogued can still be decoded
        self.auto_learn = auto_learn
        self.specs: Dict[str, LaunchpadSpec] = {spec.program_id: spec for spec in launchpads}
        self.by_key: Dict[str, LaunchpadSpec] = {spec.key: spec for spec in launchpads}
        self.prefer_onchain_idl = prefer_onchain_idl and source is not None
        self.resolve_mints = resolve_mints

        self.configs = ConfigStore(source)
        self.watcher = ProgramWatcher(
            source, list(self.specs), hash_executable=hash_executables
        ) if source is not None else ProgramWatcher(_NullSource(), list(self.specs))
        self._schema_cache = (
            SchemaCache(source, self.watcher, prefer_onchain=self.prefer_onchain_idl)
            if source is not None
            else None
        )
        self._bundled: Dict[str, Optional[ProgramSchema]] = {}
        self._static_snapshot: Dict[str, Dict[str, Any]] = {}
        self._mint_decimals: Dict[str, int] = {}
        self._learned: Dict[str, LaunchpadSpec] = {}
        self._unlearnable: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # adopting a launchpad nobody catalogued
    # ------------------------------------------------------------------
    def learn_program(self, program_id: str) -> Optional[LaunchpadSpec]:
        """Adopt an unregistered program that publishes its own IDL.

        This is what stops the launchpad table from having to be seeded by the
        very rows it is supposed to explain. A creator program nothing has
        catalogued is asked directly what its accounts look like; if it
        answers, it becomes decodable immediately.

        Returns the synthesised spec, or None when the program publishes no
        usable IDL (in which case `unlearnable` records why).
        """
        if program_id in self.specs:
            return self.specs[program_id]
        if self.source is None or program_id in self._unlearnable:
            return None

        try:
            onchain = fetch_onchain_idl(self.source, program_id)
        except Exception as exc:  # noqa: BLE001 - a bad IDL is a finding
            self._unlearnable[program_id] = f"on-chain IDL unusable: {exc}"
            return None
        if onchain is None:
            self._unlearnable[program_id] = "no IDL account at the canonical address"
            return None

        schema = onchain.compile()
        base_key = schema.name if schema.name != "unknown" else program_id[:8]
        key = f"learned:{base_key}"
        suffix = 2
        while key in self.by_key:
            key = f"learned:{base_key}-{suffix}"
            suffix += 1

        spec = LaunchpadSpec(
            key=key,
            display_name=f"{base_key} (learned from its on-chain IDL)",
            program_id=program_id,
            curve_family=CurveFamily.UNKNOWN,
            state_account="",  # the heuristic adapter ranks the candidates
            adapter="heuristic",
            requires_onchain_idl=True,
            notes="adopted at runtime; no hand-written adapter",
        )
        self.specs[program_id] = spec
        self.by_key[key] = spec
        self._learned[program_id] = spec
        self.watcher.track(program_id)
        return spec

    def _spec_for(self, program_id: str) -> Optional[LaunchpadSpec]:
        spec = self.specs.get(program_id)
        if spec is not None:
            return spec
        return self.learn_program(program_id) if self.auto_learn else None

    @property
    def learned_programs(self) -> Dict[str, LaunchpadSpec]:
        """Programs adopted at runtime rather than shipped in the registry."""
        return dict(self._learned)

    @property
    def unlearnable(self) -> Dict[str, str]:
        """Programs that could not be adopted, and why."""
        return dict(self._unlearnable)

    # ------------------------------------------------------------------
    # schemas
    # ------------------------------------------------------------------
    def bundled_schema(self, program_id: str) -> Optional[ProgramSchema]:
        if program_id not in self._bundled:
            spec = self.specs.get(program_id)
            self._bundled[program_id] = spec.bundled_schema() if spec else None
        return self._bundled[program_id]

    def schema(self, program_id: str) -> Optional[ProgramSchema]:
        """Best available schema: on-chain IDL when present, else the bundle."""
        fallback = self.bundled_schema(program_id)
        if self._schema_cache is None:
            return fallback
        return self._schema_cache.get(program_id, fallback)

    def context(self, spec: LaunchpadSpec, schema: ProgramSchema, slot: int) -> DecodeContext:
        deployment = self.watcher.deployment(spec.program_id)
        return DecodeContext(
            spec=spec,
            schema=schema,
            source=self.source,
            configs=self.configs,
            slot=slot,
            program_revision=deployment.revision() if deployment else None,
            mint_decimals=self._mint_decimals,
            resolve_mints=self.resolve_mints and self.source is not None,
        )

    # ------------------------------------------------------------------
    # decoding
    # ------------------------------------------------------------------
    def decode_account_data(
        self,
        program_id: str,
        data: bytes,
        address: Optional[str] = None,
        slot: int = 0,
    ) -> Optional[LaunchMetrics]:
        """Decode raw bytes owned by `program_id`. Returns None if not a curve."""
        spec = self._spec_for(program_id)
        if spec is None:
            return None
        schema = self.schema(program_id)
        if schema is None:
            return None
        decoded = schema.decode_account(data)
        if decoded is None:
            return None
        account_name, state = decoded

        adapter = get_adapter(spec.adapter)
        ctx = self.context(spec, schema, slot)
        wanted = adapter.state_account_names(ctx)
        if wanted and account_name not in wanted:
            return None
        return adapter.decode(ctx, account_name, state, address)

    def decode(self, account: AccountInfo) -> Optional[LaunchMetrics]:
        return self.decode_account_data(
            account.owner, account.data, account.pubkey, account.slot
        )

    def decode_address(self, address: str) -> Optional[LaunchMetrics]:
        if self.source is None:
            raise RuntimeError("decode_address needs an AccountSource")
        account = self.source.get_account(address)
        if account is None:
            return None
        return self.decode(account)

    def classify(self, account: AccountInfo) -> Optional[Tuple[str, str]]:
        """``(launchpad key, account type)`` for any account, curve or not."""
        spec = self._spec_for(account.owner)
        if spec is None:
            return None
        schema = self.schema(account.owner)
        if schema is None:
            return None
        matched = schema.identify(account.data)
        return (spec.key, matched.name) if matched else (spec.key, "")

    def scan_program(
        self, launchpad: str, *, limit: Optional[int] = None
    ) -> List[LaunchMetrics]:
        """Decode every curve account a program owns (heavy -- prefer a stream)."""
        if self.source is None:
            raise RuntimeError("scan_program needs an AccountSource")
        spec = self.by_key.get(launchpad) or get_spec(launchpad)
        schema = self.schema(spec.program_id)
        if schema is None:
            return []
        adapter = get_adapter(spec.adapter)
        ctx = self.context(spec, schema, self.source.get_slot())
        names = adapter.state_account_names(ctx)
        out: List[LaunchMetrics] = []
        for name in names or tuple(schema.accounts):
            account_schema = schema.accounts.get(name)
            if account_schema is None:
                continue
            memcmp = [(0, _b58(account_schema.discriminator))]
            for account in self.source.get_program_accounts(
                spec.program_id, memcmp=memcmp, limit=limit
            ):
                metrics = self.decode(account)
                if metrics is not None:
                    out.append(metrics)
                if limit and len(out) >= limit:
                    return out
        return out

    # ------------------------------------------------------------------
    # platforms (multi-tenant programs)
    # ------------------------------------------------------------------
    def discover_platforms(
        self, launchpad: str, *, limit: Optional[int] = None
    ) -> List[PlatformInfo]:
        """List the tenants of a multi-tenant launch program.

        LetsBonk, Believe, Bags, Jupiter Studio and friends are config accounts
        inside LaunchLab / DBC, not separate programs. Reading them off chain is
        the only way the list stays current.
        """
        if self.source is None:
            raise RuntimeError("discover_platforms needs an AccountSource")
        spec = self.by_key.get(launchpad) or get_spec(launchpad)
        schema = self.schema(spec.program_id)
        if schema is None:
            return []

        out: List[PlatformInfo] = []
        for config in spec.config_accounts:
            if not config.per_platform:
                continue
            account_schema = schema.accounts.get(config.account_type)
            if account_schema is None:
                continue
            memcmp = [(0, _b58(account_schema.discriminator))]
            for account in self.source.get_program_accounts(
                spec.program_id, memcmp=memcmp, limit=limit
            ):
                decoded = schema.decode_account(account.data)
                if decoded is None:
                    continue
                name, state = decoded
                summary = {
                    key: state.get(key)
                    for key in PLATFORM_SUMMARY_FIELDS.get(name, ())
                    if key in state
                }
                out.append(
                    PlatformInfo(
                        launchpad_key=spec.key,
                        program_id=spec.program_id,
                        config_address=account.pubkey,
                        config_account_type=name,
                        name=_decode_name(state.get("name")),
                        fields=summary,
                    )
                )
        return out

    # ------------------------------------------------------------------
    # static parameters, upgrades and self-healing
    # ------------------------------------------------------------------
    def static_params(self, launchpad: str) -> Dict[str, Any]:
        """The launch parameters a launchpad applies to *new* coins.

        These are the numbers a caller would otherwise hardcode. Everything
        returned here carries a `source`, so a value that silently fell back to
        the bundled snapshot is visible rather than assumed fresh.
        """
        spec = self.by_key.get(launchpad) or get_spec(launchpad)
        schema = self.schema(spec.program_id)
        out: Dict[str, Any] = {
            "launchpad": spec.key,
            "program_id": spec.program_id,
            "curve_family": spec.curve_family.value,
            "schema_source": schema.source if schema else "unavailable",
            "schema_version": schema.version if schema else "",
            "schema_fingerprint": schema.fingerprint if schema else "",
            "configs": {},
        }
        deployment = self.watcher.deployment(spec.program_id)
        if deployment is not None:
            out["deployment"] = summarize_deployment(deployment)

        if schema is None or self.source is None:
            return out

        for config in spec.config_accounts:
            if config.per_platform or not config.address:
                continue
            name, decoded, slot = self.configs.fetch(
                spec.program_id, config.address, schema
            )
            if decoded is None:
                continue
            out["configs"][config.role] = {
                "address": config.address,
                "account_type": name,
                "slot": slot,
                "values": {
                    key: value
                    for key, value in decoded.items()
                    if isinstance(value, (int, float, bool, str))
                },
            }
        return out

    def snapshot_static_params(
        self, launchpads: Optional[Iterable[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Record the current launch parameters as the baseline for `refresh()`.

        Polls the program watcher first so the baseline includes each program's
        deployment, otherwise the first `refresh()` would report every
        deployment as "changed" simply because it had never been read.
        """
        if self.source is not None:
            self.watcher.poll()
        keys = list(launchpads or self.by_key)
        snapshot = {}
        for key in keys:
            try:
                snapshot[key] = self.static_params(key)
            except Exception as exc:  # noqa: BLE001 - one bad pad must not stop the rest
                snapshot[key] = {"launchpad": key, "error": str(exc)}
        self._static_snapshot = snapshot
        return snapshot

    def refresh(self) -> RefreshReport:
        """Re-read deployments; on any upgrade, drop caches and diff parameters.

        Call this on a timer, or on a Geyser update for any address in
        `self.watcher.programdata_addresses()`.
        """
        report = RefreshReport()
        if self.source is None:
            return report

        if not self._static_snapshot:
            # No baseline yet: establish one (which also primes the watcher) and
            # report nothing, since "everything is new" is not a change.
            self.snapshot_static_params()
            report.unreachable = self.watcher.unreachable
            return report

        previous = dict(self._static_snapshot)
        report.upgrades = self.watcher.poll()
        report.unreachable = self.watcher.unreachable

        touched = {u.program_id for u in report.upgrades}
        for program_id in touched:
            self.configs.invalidate(program_id)
            if self._schema_cache is not None:
                self._schema_cache.invalidate(program_id)
            self._bundled.pop(program_id, None)

        keys = (
            [self.specs[pid].key for pid in touched if pid in self.specs]
            if touched
            else list(previous)
        )
        for key in keys:
            after = self.static_params(key)
            before = previous.get(key, {})
            report.changes.extend(_diff_params(key, before, after))
            self._static_snapshot[key] = after
        return report

    def deployments(self) -> Dict[str, ProgramDeployment]:
        self.watcher.poll()
        return {pid: d for pid, d in ((p, self.watcher.deployment(p)) for p in self.specs) if d}

    def onchain_idl_status(self) -> Dict[str, str]:
        """Which tracked programs publish a usable IDL on chain."""
        if self.source is None:
            return {}
        out: Dict[str, str] = {}
        for program_id, spec in self.specs.items():
            try:
                onchain = fetch_onchain_idl(self.source, program_id)
            except Exception as exc:  # noqa: BLE001
                out[spec.key] = f"error: {exc}"
                continue
            out[spec.key] = (
                f"present ({len(onchain.document.get('accounts') or [])} accounts)"
                if onchain
                else "absent"
            )
        return out


def _diff_params(key: str, before: Dict[str, Any], after: Dict[str, Any]) -> List[ParamChange]:
    changes: List[ParamChange] = []

    def walk(path: str, lhs: Any, rhs: Any) -> None:
        if isinstance(lhs, dict) and isinstance(rhs, dict):
            for name in sorted(set(lhs) | set(rhs)):
                walk(f"{path}.{name}" if path else name, lhs.get(name), rhs.get(name))
        elif lhs != rhs:
            changes.append(ParamChange(key, path, lhs, rhs))

    walk("", before, after)
    return [c for c in changes if not c.path.endswith(".slot")]


def _b58(raw: bytes) -> str:
    from .base58 import b58encode

    return b58encode(raw)


class _NullSource:
    """Stand-in so a source-less decoder can still hold a watcher."""

    def get_account(self, pubkey: str):
        return None

    def get_multiple_accounts(self, pubkeys):
        return [None] * len(pubkeys)

    def get_program_accounts(self, program_id, **kwargs):
        return []

    def get_slot(self) -> int:
        return 0
