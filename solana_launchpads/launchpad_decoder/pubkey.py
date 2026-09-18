"""PDA derivation helpers (pure Python, no `solders`/`solana-py` required).

Needed because the on-chain Anchor IDL account address is
``create_with_seed(find_program_address([], program_id), "anchor:idl", program_id)``
and the upgradeable-loader ProgramData address is
``find_program_address([program_id], BPFLoaderUpgradeable)``.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Optional, Sequence, Tuple

from .base58 import b58decode, b58encode

PDA_MARKER = b"ProgramDerivedAddress"
MAX_SEED_LEN = 32

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def is_on_curve(key: bytes) -> bool:
    """True if the 32 bytes decompress to a valid ed25519 point."""
    if len(key) != 32:
        return False
    y = int.from_bytes(key, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    if y >= _P:
        return False
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    if v == 0:
        return False
    x2 = (u * pow(v, _P - 2, _P)) % _P
    if x2 == 0:
        # x == 0 is only a valid encoding when the sign bit is clear.
        return sign == 0
    # x2 must be a quadratic residue mod p for a square root to exist.
    return pow(x2, (_P - 1) // 2, _P) == 1


def create_program_address(seeds: Sequence[bytes], program_id: str) -> Optional[str]:
    """Return the derived address, or ``None`` when it lands on the curve."""
    hasher = hashlib.sha256()
    for seed in seeds:
        if len(seed) > MAX_SEED_LEN:
            raise ValueError(f"seed too long: {len(seed)} > {MAX_SEED_LEN}")
        hasher.update(seed)
    hasher.update(b58decode(program_id))
    hasher.update(PDA_MARKER)
    digest = hasher.digest()
    if is_on_curve(digest):
        return None
    return b58encode(digest)


def find_program_address(seeds: Sequence[bytes], program_id: str) -> Tuple[str, int]:
    """Anchor/Solana ``findProgramAddress``: highest bump that is off-curve."""
    for bump in range(255, -1, -1):
        candidate = create_program_address([*seeds, bytes([bump])], program_id)
        if candidate is not None:
            return candidate, bump
    raise ValueError("unable to find a viable program address bump")


def create_with_seed(base: str, seed: str, owner: str) -> str:
    """Solana ``Pubkey::create_with_seed``."""
    seed_bytes = seed.encode()
    if len(seed_bytes) > MAX_SEED_LEN:
        raise ValueError(f"seed too long: {len(seed_bytes)} > {MAX_SEED_LEN}")
    owner_raw = b58decode(owner)
    if owner_raw[-len(PDA_MARKER) :] == PDA_MARKER:
        raise ValueError("owner cannot end with the PDA marker")
    digest = hashlib.sha256(b58decode(base) + seed_bytes + owner_raw).digest()
    return b58encode(digest)


def seeds_to_bytes(seeds: Iterable) -> list:
    """Accept str/bytes/int seeds and normalise them to bytes."""
    out = []
    for seed in seeds:
        if isinstance(seed, bytes):
            out.append(seed)
        elif isinstance(seed, str):
            # A 32-byte base58 pubkey is used raw; anything else is UTF-8.
            try:
                raw = b58decode(seed)
            except ValueError:
                raw = None
            out.append(raw if raw is not None and len(raw) == 32 else seed.encode())
        elif isinstance(seed, int):
            out.append(seed.to_bytes(8, "little"))
        else:
            raise TypeError(f"unsupported seed type {type(seed)!r}")
    return out
