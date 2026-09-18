"""Finding curves without an existing pool row.

This exists to break a bootstrap problem. If the launchpad table is *learned*
from curves that already have pool rows, then a launchpad whose coins have no
pool rows can never enter the table, and its coins can never be priced. The
gap is self-sustaining: no pool row -> no launchpad -> no pool row.

Two functions cut it, both keyed only on the creator program:

``triage_programs``
    Given the distinct ``creator_program`` values behind the unpriced coins,
    say what each one *is* and whether a price is reachable -- a known
    launchpad, a program that describes itself well enough to decode, or one
    that genuinely needs a human. That turns "33 coins we cannot price" into a
    handful of programs ranked by how many coins each unblocks.

``find_state_accounts_for_mint``
    Given a mint and its creator program, locate the curve account without an
    index, by `memcmp`-ing the mint against the exact byte offset its layout
    puts a mint field at. No pool row required, because the program's own IDL
    says where to look.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from .adapters.heuristic import map_fields
from .anchor_idl import ProgramSchema
from .base58 import b58decode, is_pubkey
from .program_state import ProgramStateError, fetch_onchain_idl, read_deployment
from .rpc import AccountInfo
from .types import CurveFamily

#: any 32-byte field whose name mentions a mint is worth a memcmp; which side
#: of the pair it is (base or quote) does not matter, since a match on either
#: still locates the account
_MINT_FIELD = re.compile(r"(^|_)mints?($|_)")

#: Launchpads that derive the curve address from the mint rather than storing
#: the mint on the curve. pump.fun is the important case: `BondingCurve`
#: carries no base mint at all, so only the PDA finds it.
_CURVE_PDA: Dict[str, str] = {
    "pumpfun": "pumpfun_bonding_curve",
    "moonit": "moonit_curve",
    "raydium_launchlab": "launchlab_pool",
}

#: what a triage verdict means for the team's backlog
KNOWN = "known_launchpad"
SELF_DESCRIBING = "self_describing"
NO_CURVE_SHAPE = "no_curve_shape"
OPAQUE = "opaque"
NOT_A_PROGRAM = "not_a_program"
UNREACHABLE = "unreachable"

_ACTION = {
    KNOWN: "already decodable -- point the decoder at the curve account",
    SELF_DESCRIBING: "decodable now via its on-chain IDL; confirm on a sample, then backfill",
    NO_CURVE_SHAPE: "publishes an IDL but no bonding-curve-shaped account -- probably not a launchpad",
    OPAQUE: "no on-chain IDL: needs an SDK, a published IDL, or reverse engineering",
    NOT_A_PROGRAM: "not an executable account -- check how creator_program was populated",
    UNREACHABLE: "could not be read from this node",
}


@dataclass
class ProgramTriage:
    """What one `creator_program` is, and whether a price is reachable."""

    program_id: str
    verdict: str
    launchpad_key: Optional[str] = None
    #: how many unpriced coins this program accounts for, when counts are given
    coin_count: int = 0
    idl_name: str = ""
    idl_version: str = ""
    curve_accounts: List[str] = field(default_factory=list)
    best_confidence: float = 0.0
    curve_family: str = CurveFamily.UNKNOWN.value
    upgrade_authority: Optional[str] = None
    last_deploy_slot: Optional[int] = None
    note: str = ""

    @property
    def decodable(self) -> bool:
        return self.verdict in (KNOWN, SELF_DESCRIBING)

    @property
    def action(self) -> str:
        return _ACTION.get(self.verdict, "")

    def as_dict(self) -> Dict:
        return {
            "program_id": self.program_id,
            "verdict": self.verdict,
            "decodable": self.decodable,
            "coin_count": self.coin_count,
            "launchpad": self.launchpad_key,
            "idl": f"{self.idl_name} {self.idl_version}".strip(),
            "curve_accounts": self.curve_accounts,
            "best_confidence": round(self.best_confidence, 3),
            "curve_family": self.curve_family,
            "upgrade_authority": self.upgrade_authority,
            "last_deploy_slot": self.last_deploy_slot,
            "action": self.action,
            "note": self.note,
        }


def _curve_shaped_accounts(schema: ProgramSchema):
    """Rank a program's accounts by how much they look like bonding-curve state."""
    ranked = []
    for name, account in schema.accounts.items():
        fields = {field_name: 0 for field_name, _ in account.layout.fields}
        mapping = map_fields(fields)
        if mapping.confidence > 0:
            ranked.append((mapping.confidence, name, mapping.family))
    ranked.sort(reverse=True)
    return ranked


def triage_programs(
    decoder,
    program_ids: Iterable[str],
    *,
    coin_counts: Optional[Dict[str, int]] = None,
) -> List[ProgramTriage]:
    """Classify each creator program. Sorted by how many coins it unblocks.

    `decoder` is a `LaunchpadDecoder`; a node is required, since the whole
    point is to ask the chain about programs nothing has catalogued yet.
    """
    counts = coin_counts or {}
    results: List[ProgramTriage] = []

    for program_id in dict.fromkeys(program_ids):
        entry = ProgramTriage(
            program_id=program_id,
            verdict=OPAQUE,
            coin_count=counts.get(program_id, 0),
        )

        if not is_pubkey(program_id):
            entry.verdict = NOT_A_PROGRAM
            entry.note = "not a valid base58 pubkey"
            results.append(entry)
            continue

        spec = decoder.specs.get(program_id)
        if spec is not None and program_id not in decoder.learned_programs:
            entry.verdict = KNOWN
            entry.launchpad_key = spec.key
            entry.curve_family = spec.curve_family.value
            entry.curve_accounts = [spec.state_account] if spec.state_account else []
            entry.best_confidence = 1.0
            entry.note = f"registered launchpad ({spec.display_name})"
            results.append(entry)
            continue

        if decoder.source is None:
            results.append(entry)
            continue

        # Is this even a program? A creator_program column can pick up a mint,
        # a wallet or a PDA if it was populated from the wrong instruction key.
        account = decoder.source.get_account(program_id)
        if account is None:
            entry.verdict = UNREACHABLE
            entry.note = "account not found on this node"
            results.append(entry)
            continue
        if not account.executable:
            entry.verdict = NOT_A_PROGRAM
            entry.note = f"account exists but is not executable (owner {account.owner})"
            results.append(entry)
            continue

        try:
            deployment = read_deployment(decoder.source, program_id)
            entry.upgrade_authority = deployment.upgrade_authority
            entry.last_deploy_slot = deployment.last_deploy_slot
        except ProgramStateError as exc:
            entry.note = str(exc)

        try:
            onchain = fetch_onchain_idl(decoder.source, program_id)
        except Exception as exc:  # noqa: BLE001 - a bad IDL is a finding, not a crash
            entry.note = f"on-chain IDL present but unusable: {exc}"
            results.append(entry)
            continue

        if onchain is None:
            entry.verdict = OPAQUE
            entry.note = "no IDL account at the canonical address"
            results.append(entry)
            continue

        schema = onchain.compile()
        entry.idl_name = schema.name
        entry.idl_version = schema.version
        ranked = _curve_shaped_accounts(schema)
        if not ranked:
            entry.verdict = NO_CURVE_SHAPE
            entry.note = f"IDL has {len(schema.accounts)} account type(s), none curve-shaped"
        else:
            entry.verdict = SELF_DESCRIBING
            entry.best_confidence = ranked[0][0]
            entry.curve_family = ranked[0][2].value
            entry.curve_accounts = [name for _score, name, _family in ranked]
        results.append(entry)

    results.sort(key=lambda r: (-r.coin_count, -r.best_confidence, r.program_id))
    return results


def summarise_triage(results: Sequence[ProgramTriage]) -> Dict[str, int]:
    """Coin counts per verdict -- the 'few programs or a long tail' answer."""
    coins = Counter()
    programs = Counter()
    for entry in results:
        coins[entry.verdict] += entry.coin_count
        programs[entry.verdict] += 1
    return {
        "programs": dict(programs),
        "coins": dict(coins),
        "programs_total": len(results),
        "coins_total": sum(entry.coin_count for entry in results),
        "coins_decodable": sum(e.coin_count for e in results if e.decodable),
        "programs_decodable": sum(1 for e in results if e.decodable),
    }


@dataclass
class CurveCandidate:
    address: str
    account_type: str
    program_id: str
    matched_field: str
    memcmp_offset: int


def mint_field_offsets(schema: ProgramSchema) -> List[tuple]:
    """(account type, field, account-relative offset) for every mint-ish field."""
    out = []
    for name, account in schema.accounts.items():
        for field_name in account.layout.pubkey_fields():
            if not _MINT_FIELD.search(field_name):
                continue
            offset = account.layout.memcmp_offset(field_name)
            if offset is not None:
                out.append((name, field_name, offset))
    return out


def derived_curve_address(decoder, program_id: str, mint: str) -> Optional[str]:
    """The curve address a launchpad derives from the mint, when it does."""
    spec = decoder.specs.get(program_id)
    if spec is None:
        return None
    helper_name = _CURVE_PDA.get(spec.key)
    if helper_name is None:
        return None
    from . import pdas

    helper = getattr(pdas, helper_name, None)
    if helper is None:
        return None
    try:
        return helper(mint)
    except Exception:  # noqa: BLE001 - a bad mint is caught by the caller
        return None


def find_state_accounts_for_mint(
    decoder,
    program_id: str,
    mint: str,
    *,
    limit: Optional[int] = 25,
) -> List[CurveCandidate]:
    """Locate a program's state account for `mint`, with no pool row or index.

    Two routes, cheapest first:

    1. **PDA derivation**, when the launchpad derives the curve address from
       the mint. pump.fun is exactly this case -- its `BondingCurve` stores no
       base mint, so a memcmp can never find it and only the PDA works.
    2. **A server-side `memcmp`** against the byte offset the program's own
       layout puts a mint field at -- one filtered `getProgramAccounts` per
       candidate field, not a full program scan.
    """
    if decoder.source is None:
        raise RuntimeError("find_state_accounts_for_mint needs an AccountSource")
    schema = decoder.schema(program_id)
    if schema is None:
        return []

    b58decode(mint)  # fail fast on a malformed mint
    found: Dict[str, CurveCandidate] = {}

    derived = derived_curve_address(decoder, program_id, mint)
    if derived:
        account = decoder.source.get_account(derived)
        if account is not None and account.owner == program_id and account.data:
            matched = schema.identify(account.data)
            found[derived] = CurveCandidate(
                address=derived,
                account_type=matched.name if matched else "",
                program_id=program_id,
                matched_field="(PDA of mint)",
                memcmp_offset=-1,
            )

    for account_type, field_name, offset in mint_field_offsets(schema):
        account_schema = schema.accounts[account_type]
        from .base58 import b58encode

        filters = [
            (0, b58encode(account_schema.discriminator)),
            (offset, mint),
        ]
        try:
            rows = decoder.source.get_program_accounts(
                program_id, memcmp=filters, limit=limit
            )
        except Exception:  # noqa: BLE001 - a node that refuses one filter can serve another
            continue
        for row in rows:
            if row.pubkey in found:
                continue
            found[row.pubkey] = CurveCandidate(
                address=row.pubkey,
                account_type=account_type,
                program_id=program_id,
                matched_field=field_name,
                memcmp_offset=offset,
            )
    return list(found.values())


def price_mint(decoder, program_id: str, mint: str):
    """Locate and decode the curve for a mint -- the end-to-end backfill call.

    Returns the first `LaunchMetrics` found, or None when the program owns no
    state account referencing the mint.
    """
    for candidate in find_state_accounts_for_mint(decoder, program_id, mint):
        metrics = decoder.decode_address(candidate.address)
        if metrics is not None:
            return metrics
    return None


def classify_accounts(decoder, accounts: Iterable[AccountInfo]) -> Dict[str, int]:
    """Count account types across a batch -- useful for auditing a backfill."""
    counts: Counter = Counter()
    for account in accounts:
        label = decoder.classify(account)
        counts["unknown_program" if label is None else f"{label[0]}:{label[1] or '?'}"] += 1
    return dict(counts)
