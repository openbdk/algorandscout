# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Input validation — reject malformed identifiers here, not at the upstream.

Two reasons this is not merely a nicety:

1. **Correct blame.** Without it, a typo'd address travels to the indexer, comes
   back 400, and the service reports 502 — telling an operator the indexer is
   broken when the caller simply mistyped. That misdirects incident response and
   pollutes the upstream error budget with client mistakes.
2. **Cost.** A malformed identifier cannot possibly match anything. Spending a
   network round trip (and, on metered providers, a credit) to be told so is
   waste that scales with the number of bad callers.

Algorand addresses carry their own checksum, so a single transposed character is
detectable locally with certainty. That is a rare luxury and worth using.
"""

from __future__ import annotations

import base64
import hashlib
import re

#: RFC-4648 base32 — note the absent 0, 1, 8 and 9.
BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_BASE32_RE = re.compile(rf"^[{BASE32_ALPHABET}]+$")

ADDRESS_LENGTH = 58  # 32-byte public key + 4-byte checksum, base32, unpadded
TXID_LENGTH = 52  # 32-byte hash, base32, unpadded

#: Algorand IDs are uint64 on the wire.
MAX_UINT64 = 2**64 - 1


class ValidationError(ValueError):
    """The caller's input is malformed. Always a 4xx, never a 5xx."""


def _b32_decode(value: str, expected_bytes: int) -> bytes:
    padding = "=" * (-len(value) % 8)
    raw = base64.b32decode(value + padding)
    if len(raw) != expected_bytes:
        raise ValidationError(f"decoded to {len(raw)} bytes, expected {expected_bytes}")
    return raw


def is_valid_address(address: str) -> bool:
    try:
        validate_address(address)
        return True
    except ValidationError:
        return False


def validate_address(address: str) -> str:
    """
    Validate an Algorand account address, checksum included.

    An address is base32 of (32-byte Ed25519 public key ‖ 4-byte checksum), where
    the checksum is the last 4 bytes of SHA-512/256 over the public key. Checking
    it catches transposition and single-character typos outright.
    """
    if not isinstance(address, str) or not address:
        raise ValidationError("address must be a non-empty string")

    if len(address) != ADDRESS_LENGTH:
        raise ValidationError(f"address must be {ADDRESS_LENGTH} characters, got {len(address)}")

    if not _BASE32_RE.match(address):
        bad = sorted({ch for ch in address if ch not in BASE32_ALPHABET})
        raise ValidationError(
            f"address contains characters outside the base32 alphabet: {' '.join(bad)} "
            "(note that 0, 1, 8 and 9 are not base32 digits)"
        )

    try:
        raw = _b32_decode(address, 36)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"address is not decodable base32: {exc}") from exc

    public_key, checksum = raw[:32], raw[32:]
    expected = hashlib.new("sha512_256", public_key).digest()[-4:]
    if checksum != expected:
        raise ValidationError("address checksum does not match — likely a typo or truncation")

    return address


def validate_txid(txid: str) -> str:
    """Validate a transaction id: base32 of a 32-byte hash. No checksum exists here."""
    if not isinstance(txid, str) or not txid:
        raise ValidationError("transaction id must be a non-empty string")

    if len(txid) != TXID_LENGTH:
        raise ValidationError(f"transaction id must be {TXID_LENGTH} characters, got {len(txid)}")

    if not _BASE32_RE.match(txid):
        raise ValidationError("transaction id contains characters outside the base32 alphabet")

    try:
        _b32_decode(txid, 32)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"transaction id is not decodable base32: {exc}") from exc

    return txid


def validate_uint64(value: int, *, name: str = "id") -> int:
    """Asset ids, application ids and rounds are all uint64 on the wire."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer")
    if value < 0:
        raise ValidationError(f"{name} must not be negative, got {value}")
    if value > MAX_UINT64:
        raise ValidationError(f"{name} exceeds uint64 range, got {value}")
    return value


def classify_query(query: str) -> str | None:
    """
    Identify what a search term *is*, by shape.

    Returns "address" | "transaction" | "numeric" | None (treat as a name/unit
    search). Used so search probes only the namespaces a term could belong to,
    instead of firing every lookup at every term.
    """
    term = (query or "").strip()
    if not term:
        return None
    if len(term) == ADDRESS_LENGTH and _BASE32_RE.match(term):
        return "address"
    if len(term) == TXID_LENGTH and _BASE32_RE.match(term):
        return "transaction"
    if term.isdigit():
        return "numeric"
    return None
