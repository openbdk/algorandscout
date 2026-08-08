# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
What this API can and cannot answer.

A read surface that silently returns a plausible-looking `null` for a question the
chain cannot answer is worse than one that refuses. Several concepts that are treated
as universal across blockchain tooling — gas, nonces, indexed log topics, reorgs — do
not exist on Algorand at all. This file is the machine-readable statement of that
boundary; the service exposes it at `GET /api/v2/capabilities` so a client can find out
*before* it builds a query on a field that will never be populated.
"""

from __future__ import annotations

from typing import Any

MODULE_VERSION = "1.0.0"

#: Verified against the live mainnet indexer on 2026-08-08.
CHAIN = {
    "name": "Algorand",
    "family": "avm",
    "is_evm": False,
    "native_currency": {"symbol": "ALGO", "decimals": 6, "base_unit": "microAlgo"},
    "address_format": "base32-ed25519-58char",
    "finality": "instant",
    "reorgs": False,
    "networks": {
        "mainnet": {"genesis_id": "mainnet-v1.0", "genesis_hash": "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="},
        "testnet": {"genesis_id": "testnet-v1.0"},
        "betanet": {"genesis_id": "betanet-v1.0"},
        "localnet": {"genesis_id": "dockernet-v1"},
    },
}

#: What this API serves, and what each thing is actually made of on Algorand.
SUPPORTED: dict[str, str] = {
    "blocks": "Algorand rounds. One round is one block; rounds are final on write.",
    "transactions": "Algorand transactions, all seven types (pay, keyreg, acfg, axfer, afrz, appl, stpf).",
    "internal_transactions": "Inner transactions emitted by application calls — a genuine analogue.",
    "addresses": "Algorand accounts, addressed by 58-character base32 (not 20-byte hex).",
    "token_balances": "ASA holdings (`/v2/accounts/{addr}/assets`).",
    "tokens": "Algorand Standard Assets. Fungible and non-fungible alike.",
    "token_transfers": "Asset-transfer (axfer) transactions.",
    "smart_contracts": "Algorand applications (app IDs) with TEAL/AVM approval and clear-state programs, declared state schemas and decoded global state.",
    "search": "Asset unit-name/name search, plus exact address / txid / app-id / round resolution.",
    "stats": "Chain tip, indexer lag, block time.",
}

#: Concepts a client may ask for that Algorand cannot supply. The value is the reason,
#: and the reason is always structural — not "not implemented yet".
UNSUPPORTED: dict[str, str] = {
    "logs_by_topic": (
        "Algorand application logs are an ordered array of opaque byte strings. There are no "
        "indexed topics, so there is nothing to filter on. Topic-style queries cannot be "
        "emulated without full-scanning and guessing."
    ),
    "gas_price": (
        "Algorand has no gas market. Fees are flat per transaction (minimum 1000 microAlgos, "
        "raised only by congestion) and compute is bounded by a fixed opcode budget, not purchased."
    ),
    "gas_used": "See gas_price. `fee` in microAlgos is reported instead and is not the same quantity.",
    "contract_abi": (
        "Applications are TEAL/AVM programs. Where a contract follows ARC-4 an ABI-like method "
        "surface exists, but it is not stored on chain by default and cannot be recovered from "
        "the compiled program."
    ),
    "contract_source_verification": (
        "There is no public verified-source registry for AVM programs. The approval and "
        "clear-state programs are available as compiled bytecode only."
    ),
    "eth_call": (
        "There is no read-only VM invocation over the public indexer. Application *state* is "
        "readable directly (global state, local state, boxes) which answers most of the same "
        "questions without executing anything."
    ),
    "token_allowance": (
        "ASAs have no allowance/approve model. Delegated spending is expressed with clawback "
        "addresses and logic signatures, which are not equivalent and must not be presented as such."
    ),
    "reorg_handling": "Algorand blocks are final on write. There is no reorg depth to expose.",
    "uncle_blocks": (
        "Algorand's consensus produces one block per round with immediate finality, so no "
        "competing or orphaned blocks are ever produced. There is no uncle/ommer set to expose, "
        "and an empty list here means 'structurally impossible', not 'none this round'."
    ),
    "nonce": (
        "Algorand replay protection uses a first-valid/last-valid round window plus the genesis "
        "hash, not a monotonic per-account nonce."
    ),
}

#: Where a naive one-to-one mapping would be actively misleading.
CAVEATS: dict[str, str] = {
    "address_hash": (
        "`hash` carries the 58-character base32 Algorand address verbatim. Clients that validate "
        "against `^0x[0-9a-fA-F]{40}$` will reject it. That rejection is correct behaviour and this "
        "API will not fabricate a hex-shaped address to satisfy it."
    ),
    "nft_detection": (
        "An ASA with total=1 and decimals=0 is *conventionally* a non-fungible token (ARC-3 / "
        "ARC-19 / ARC-69). This module reports that as a heuristic and labels it as one. It is not "
        "an on-chain type distinction."
    ),
    "token_symbol": (
        "ASA unit-names are not unique — anyone can mint an asset called USDC. Symbol lookup "
        "returns candidates, ranked by nothing. Resolve by asset ID, creator, and holder count."
    ),
    "decimals": "ALGO amounts are microAlgos (10^-6). ASA decimals are per-asset and may be 0.",
    "rewards": (
        "`amount` on an account already includes pending rewards in current protocol versions; "
        "`amount-without-pending-rewards` is retained for older data and the two may differ on "
        "historical rounds."
    ),
    "indexer_lag": (
        "The indexer trails the node. Any answer sourced from history is as-of `indexer_round`, "
        "not the chain tip. The service reports both."
    ),
}


def capabilities() -> dict[str, Any]:
    """The full statement, as served at `/api/v2/capabilities`."""
    return {
        "module": "algorandscout",
        "version": MODULE_VERSION,
        "license": "BANKON (Apache-2.0)",
        "chain": CHAIN,
        "supported": SUPPORTED,
        "unsupported": UNSUPPORTED,
        "caveats": CAVEATS,
        "notice": (
            "Algorandscout is an independent Algorand explorer API, part of the Open Blockchain "
            "Development Kit, licensed under the BANKON License. Its REST layout follows "
            "conventions common to explorer APIs so existing tooling interoperates, but Algorand's "
            "model governs: see `unsupported` before assuming a field exists."
        ),
    }


def is_supported(feature: str) -> bool:
    return feature in SUPPORTED


def why_unsupported(feature: str) -> str | None:
    return UNSUPPORTED.get(feature)
