# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Algorand → Blockscout-shaped DTOs.

Every function here is pure: dict in, dict out, no I/O. That is deliberate — the mapping
is the part most likely to be wrong, so it is the part that must be testable without a
network.

The shapes follow Blockscout's `/api/v2` response conventions closely enough that a
client written against them keeps working, and every field that has no honest Algorand
equivalent is either omitted or carries an explicit `null` alongside an entry in
`capabilities.UNSUPPORTED`. Nothing here invents a value to fill a hole.

Field names on the Algorand side are hyphenated (`confirmed-round`, `asset-id`) because
that is what the indexer returns; they are verified against live mainnet responses
captured 2026-08-08 and kept in `tests/fixtures/`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

ALGO_DECIMALS = 6
MICRO_ALGO = 10**6

#: Algorand transaction type → the sub-object the indexer nests its detail under.
TX_TYPE_DETAIL_KEY = {
    "pay": "payment-transaction",
    "axfer": "asset-transfer-transaction",
    "acfg": "asset-config-transaction",
    "afrz": "asset-freeze-transaction",
    "appl": "application-transaction",
    "keyreg": "keyreg-transaction",
    "stpf": "state-proof-transaction",
    "hb": "heartbeat-transaction",
}

#: Human labels, used where Blockscout would show a decoded method name.
TX_TYPE_LABEL = {
    "pay": "Payment",
    "axfer": "Asset Transfer",
    "acfg": "Asset Configuration",
    "afrz": "Asset Freeze",
    "appl": "Application Call",
    "keyreg": "Key Registration",
    "stpf": "State Proof",
    "hb": "Heartbeat",
}

ZERO_ADDRESS = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ"


# ------------------------------------------------------------------ primitives


def iso(timestamp: Optional[int]) -> Optional[str]:
    """Unix seconds → ISO-8601 UTC. Algorand round-times are seconds, not milliseconds."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def decimal_string(amount: Optional[int], decimals: int) -> Optional[str]:
    """
    Integer base units → decimal string, without float. Amounts on Algorand routinely
    exceed 2^53 (the USDC ASA total is 2^64-1), so anything that touches a float here is
    a bug waiting for a big holder.
    """
    if amount is None:
        return None
    if decimals <= 0:
        return str(amount)
    sign = "-" if amount < 0 else ""
    digits = str(abs(amount)).rjust(decimals + 1, "0")
    whole, frac = digits[:-decimals], digits[-decimals:]
    frac = frac.rstrip("0")
    return f"{sign}{whole}.{frac}" if frac else f"{sign}{whole}"


def is_zero_address(address: Optional[str]) -> bool:
    return address == ZERO_ADDRESS


def _clean(address: Optional[str]) -> Optional[str]:
    """The all-zero address means 'unset' in ASA params, not 'owned by nobody'."""
    return None if address is None or is_zero_address(address) else address


# --------------------------------------------------------------------- address


def map_account(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Indexer `/v2/accounts/{address}` → Blockscout-shaped address object.

    `hash` is the Algorand address verbatim. See `capabilities.CAVEATS["address_hash"]`.
    """
    account = payload.get("account", payload) or {}
    address = account.get("address")
    total_created_apps = account.get("total-created-apps", 0) or 0

    return {
        "hash": address,
        "hash_format": "base32-ed25519",
        "coin_balance": str(account.get("amount", 0)),
        "coin_balance_decimal": decimal_string(account.get("amount", 0), ALGO_DECIMALS),
        "is_contract": total_created_apps > 0,
        "is_verified": None,  # no verified-source registry for AVM programs
        "name": None,
        "status": account.get("status"),
        "signature_type": account.get("sig-type"),
        "rekeyed_to": _clean(account.get("auth-addr")),
        "min_balance": account.get("min-balance"),
        "created_at_round": account.get("created-at-round"),
        "deleted": account.get("deleted", False),
        "counters": {
            "assets_held": account.get("total-assets-opted-in", 0),
            "assets_created": account.get("total-created-assets", 0),
            "apps_opted_in": account.get("total-apps-opted-in", 0),
            "apps_created": total_created_apps,
            "boxes": account.get("total-boxes", 0),
            "box_bytes": account.get("total-box-bytes", 0),
        },
        "as_of_round": payload.get("current-round") or account.get("round"),
        # Absent by structure, not by omission:
        "nonce": None,
        "implementation_address": None,
    }


def map_account_assets(payload: dict[str, Any], asset_params: Optional[dict[int, dict]] = None) -> dict[str, Any]:
    """
    Indexer `/v2/accounts/{addr}/assets` → Blockscout-shaped token-balance list.

    `asset_params` optionally supplies resolved ASA metadata keyed by asset id; without
    it the entries carry the holding only, because the holding record itself does not
    include the asset's name or decimals.
    """
    params = asset_params or {}
    items = []
    for holding in payload.get("assets", []) or []:
        asset_id = holding.get("asset-id")
        meta = params.get(asset_id, {})
        decimals = meta.get("decimals")
        items.append(
            {
                "token": _token_stub(asset_id, meta),
                "value": str(holding.get("amount", 0)),
                "value_decimal": decimal_string(holding.get("amount", 0), decimals) if decimals is not None else None,
                "is_frozen": holding.get("is-frozen", False),
                "opted_in_at_round": holding.get("opted-in-at-round"),
                "deleted": holding.get("deleted", False),
            }
        )
    return {"items": items, "next_page_params": _next_page(payload)}


# ----------------------------------------------------------------------- token


def _token_stub(asset_id: Optional[int], params: dict[str, Any]) -> dict[str, Any]:
    total = params.get("total")
    decimals = params.get("decimals")
    return {
        "address": str(asset_id) if asset_id is not None else None,
        "asset_id": asset_id,
        "name": params.get("name"),
        "symbol": params.get("unit-name"),
        "decimals": decimals,
        "total_supply": str(total) if total is not None else None,
        "type": classify_asset(params),
        "icon_url": None,
    }


def classify_asset(params: dict[str, Any]) -> str:
    """
    ASA → a Blockscout-style token type.

    Heuristic, and labelled as one. Algorand has a single asset primitive; the
    NFT/fungible distinction is a *convention* (total=1, decimals=0 per ARC-3/19/69),
    not an on-chain type. `ASA` is returned whenever the convention does not clearly apply.
    """
    total = params.get("total")
    decimals = params.get("decimals")
    if total == 1 and decimals == 0:
        return "ASA-NFT"  # ARC-3 / ARC-19 / ARC-69 convention
    if isinstance(total, int) and total > 1 and decimals == 0 and total <= 10_000:
        return "ASA-NFT-EDITION"  # fractional / edition convention
    return "ASA"


def map_asset(payload: dict[str, Any]) -> dict[str, Any]:
    """Indexer `/v2/assets/{id}` → Blockscout-shaped token object."""
    asset = payload.get("asset", payload) or {}
    params = asset.get("params", {}) or {}
    total = params.get("total")
    decimals = params.get("decimals", 0)

    return {
        "address": str(asset.get("index")),
        "asset_id": asset.get("index"),
        "name": params.get("name"),
        "symbol": params.get("unit-name"),
        "decimals": decimals,
        "type": classify_asset(params),
        "total_supply": str(total) if total is not None else None,
        "total_supply_decimal": decimal_string(total, decimals) if total is not None else None,
        "url": params.get("url"),
        "metadata_hash": params.get("metadata-hash"),
        "default_frozen": params.get("default-frozen", False),
        "created_at_round": asset.get("created-at-round"),
        "destroyed_at_round": asset.get("destroyed-at-round"),
        "deleted": asset.get("deleted", False),
        # Algorand's four privileged roles. No ERC-20 analogue exists; presenting them as
        # "owner" would understate clawback, which can move a holder's balance without consent.
        "roles": {
            "creator": params.get("creator"),
            "manager": _clean(params.get("manager")),
            "reserve": _clean(params.get("reserve")),
            "freeze": _clean(params.get("freeze")),
            "clawback": _clean(params.get("clawback")),
        },
        "clawback_enabled": _clean(params.get("clawback")) is not None,
        "freeze_enabled": _clean(params.get("freeze")) is not None,
        "as_of_round": payload.get("current-round"),
        "holders_count": None,  # requires a full balance walk; not free
    }


# ------------------------------------------------------------------- contract


def map_application(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Indexer `/v2/applications/{id}` → Blockscout-shaped smart-contract object.

    `approval-program` / `clear-state-program` are base64 AVM bytecode. There is no
    Solidity ABI and no verified-source registry — both fields are explicitly null
    rather than absent, so a client can tell the difference between "no data" and
    "field missing from this response".
    """
    app = payload.get("application", payload) or {}
    params = app.get("params", {}) or {}
    global_state = params.get("global-state", []) or []

    return {
        "address": str(app.get("id")),
        "app_id": app.get("id"),
        "creator_address_hash": params.get("creator"),
        "is_contract": True,
        "language": "TEAL/AVM",
        "approval_program": params.get("approval-program"),
        "clear_state_program": params.get("clear-state-program"),
        "extra_program_pages": params.get("extra-program-pages", 0),
        "created_at_round": app.get("created-at-round"),
        "deleted_at_round": app.get("deleted-at-round"),
        "deleted": app.get("deleted", False),
        "state_schema": {
            "global": params.get("global-state-schema"),
            "local": params.get("local-state-schema"),
        },
        "global_state": [_decode_state_entry(e) for e in global_state],
        # Structurally unavailable — see capabilities.UNSUPPORTED
        "abi": None,
        "source_code": None,
        "is_verified": False,
        "compiler_version": None,
        "optimization_enabled": None,
        "as_of_round": payload.get("current-round"),
    }


def _decode_state_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Global-state entries are base64 keys with a tagged value union
    (`type` 1 = bytes, 2 = uint). The raw base64 is preserved alongside any decode,
    because a "key" that fails to be UTF-8 is still a real key.
    """
    import base64

    raw_key = entry.get("key", "")
    try:
        decoded = base64.b64decode(raw_key).decode("utf-8")
        key = decoded if decoded.isprintable() else None
    except Exception:  # noqa: BLE001 — any decode failure means "not text", which is fine
        key = None

    value = entry.get("value", {}) or {}
    return {
        "key_b64": raw_key,
        "key": key,
        "type": "bytes" if value.get("type") == 1 else "uint",
        "value": value.get("bytes") if value.get("type") == 1 else value.get("uint"),
    }


# ----------------------------------------------------------------------- block


def map_block(payload: dict[str, Any], *, include_transactions: bool = False) -> dict[str, Any]:
    """
    Indexer `/v2/blocks/{round}` → Blockscout-shaped block object.

    Algorand rounds are final on write, so there is no reorg depth, no uncle list, and
    no difficulty. `proposer` is the analogue of `miner`.
    """
    transactions = payload.get("transactions", []) or []
    block = {
        "height": payload.get("round"),
        # The indexer does not return a round's *own* block hash — only its parent's. A
        # block is addressed by round number on Algorand. Filling `hash` with the parent
        # hash would be a plausible-looking lie, so it stays null and `parent_hash` is
        # the only hash reported.
        "hash": None,
        "parent_hash": payload.get("previous-block-hash"),
        "timestamp": iso(payload.get("timestamp")),
        "timestamp_unix": payload.get("timestamp"),
        "miner": {"hash": payload.get("proposer")},
        "proposer": payload.get("proposer"),
        "transactions_count": len(transactions),
        "transactions_root": payload.get("transactions-root"),
        "seed": payload.get("seed"),
        "genesis_id": payload.get("genesis-id"),
        "fees_collected": payload.get("fees-collected"),
        "fees_collected_decimal": decimal_string(payload.get("fees-collected"), ALGO_DECIMALS),
        "proposer_payout": payload.get("proposer-payout"),
        "bonus": payload.get("bonus"),
        "rewards": payload.get("rewards"),
        "upgrade_state": payload.get("upgrade-state"),
        # Structurally absent on Algorand:
        "difficulty": None,
        "total_difficulty": None,
        "gas_used": None,
        "gas_limit": None,
        "base_fee_per_gas": None,
        "uncles_hashes": [],
        "is_final": True,
    }
    if include_transactions:
        block["transactions"] = [map_transaction(t) for t in transactions]
    return block


# ----------------------------------------------------------------- transaction


def map_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    """
    Indexer transaction → Blockscout-shaped transaction object.

    A single Algorand transaction carries exactly one typed sub-object. The mapper reads
    that sub-object rather than guessing from top-level fields, and surfaces the
    type-specific detail under `algorand` so nothing is lost in translation.
    """
    tx_type = tx.get("tx-type")
    detail_key = TX_TYPE_DETAIL_KEY.get(tx_type or "", "")
    detail = tx.get(detail_key, {}) or {}
    confirmed = tx.get("confirmed-round")

    to_address, value, token_transfer = _extract_movement(tx_type, detail)
    close = _extract_close(tx_type, detail, tx)

    inner = tx.get("inner-txns", []) or []

    return {
        "hash": tx.get("id"),
        "block_number": confirmed,
        "timestamp": iso(tx.get("round-time")),
        "timestamp_unix": tx.get("round-time"),
        "from": {"hash": tx.get("sender")},
        "to": {"hash": to_address} if to_address else None,
        "value": str(value) if value is not None else "0",
        "value_decimal": decimal_string(value, ALGO_DECIMALS) if tx_type == "pay" and value is not None else None,
        # `value` is what the sender explicitly moved. A closing transaction ALSO sweeps the
        # account's entire remaining balance to `close_remainder_to` — funds that never appear
        # in `amount`. Reporting only `value` understates the movement and hides the closure,
        # the same class of omission as flattening a clawback into an ordinary transfer. Both
        # numbers are reported: `value` keeps its meaning, `value_total` is what actually left.
        "close": close,
        "closes_account": close["closes_account"] if close else False,
        "value_total": close["value_total"] if close else (str(value) if value is not None else "0"),
        "fee": str(tx.get("fee", 0)),
        "fee_decimal": decimal_string(tx.get("fee", 0), ALGO_DECIMALS),
        "status": "ok" if confirmed else "pending",
        "method": TX_TYPE_LABEL.get(tx_type or "", tx_type),
        "tx_type": tx_type,
        "confirmations": None,  # caller supplies: chain tip - confirmed-round
        "position": tx.get("intra-round-offset"),
        "note": tx.get("note"),
        "group": tx.get("group"),
        "rekey_to": _clean(tx.get("rekey-to")),
        "signature_type": _signature_type(tx.get("signature", {}) or {}),
        "validity": {"first": tx.get("first-valid"), "last": tx.get("last-valid")},
        "token_transfer": token_transfer,
        "created_asset_index": tx.get("created-asset-index"),
        "created_application_index": tx.get("created-application-index"),
        "logs": tx.get("logs"),  # opaque byte strings; NO topics — see capabilities
        "internal_transactions_count": len(inner),
        "internal_transactions": [map_transaction(i) for i in inner] if inner else [],
        "algorand": {detail_key: detail} if detail_key and detail else {},
        # Structurally absent:
        "gas_used": None,
        "gas_price": None,
        "gas_limit": None,
        "nonce": None,
        "revert_reason": None,
    }


def _extract_movement(tx_type: Optional[str], detail: dict[str, Any]) -> tuple[Optional[str], Optional[int], Optional[dict]]:
    """Returns (counterparty, native value, token-transfer block) for the given type."""
    if tx_type == "pay":
        return detail.get("receiver"), detail.get("amount", 0), None

    if tx_type == "axfer":
        transfer = {
            "asset_id": detail.get("asset-id"),
            "from": detail.get("sender") or None,
            "to": detail.get("receiver"),
            "amount": str(detail.get("amount", 0)),
            "close_to": _clean(detail.get("close-to")),
            # A clawback moves funds from a holder without the holder signing. Naming it
            # matters: presented as an ordinary transfer it would misrepresent consent.
            "is_clawback": bool(detail.get("sender")),
        }
        return detail.get("receiver"), 0, transfer

    if tx_type == "appl":
        app_id = detail.get("application-id")
        return (str(app_id) if app_id else None), 0, None

    if tx_type == "acfg":
        asset_id = detail.get("asset-id")
        return (str(asset_id) if asset_id else None), 0, None

    if tx_type == "afrz":
        return detail.get("address"), 0, None

    return None, 0, None


def _extract_close(tx_type: Optional[str], detail: dict[str, Any], tx: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Account/holding closure — the movement that hides inside a transaction's fine print.

    A `pay` with `close-remainder-to` sweeps the sender's **entire remaining balance** to that
    address and closes the account; the swept amount is `close-amount`, never `amount`. An
    `axfer` with `close-to` does the same for one ASA holding and closes the opt-in.

    Returns None when nothing closes, so `closes_account` is false by absence rather than by a
    field that has to be checked for truthiness.
    """
    if tx_type == "pay":
        target = _clean(detail.get("close-remainder-to"))
        if not target:
            return None
        amount = detail.get("amount", 0) or 0
        close_amount = detail.get("close-amount", tx.get("closing-amount", 0)) or 0
        return {
            "closes_account": True,
            "close_remainder_to": target,
            "close_amount": str(close_amount),
            "close_amount_decimal": decimal_string(close_amount, ALGO_DECIMALS),
            "value_total": str(amount + close_amount),
            "value_total_decimal": decimal_string(amount + close_amount, ALGO_DECIMALS),
            "asset_id": None,
        }

    if tx_type == "axfer":
        target = _clean(detail.get("close-to"))
        if not target:
            return None
        amount = detail.get("amount", 0) or 0
        close_amount = detail.get("close-amount", 0) or 0
        return {
            # An axfer close ends an ASA opt-in, not the Algorand account itself.
            "closes_account": False,
            "closes_asset_holding": True,
            "close_remainder_to": target,
            "close_amount": str(close_amount),
            "close_amount_decimal": None,  # ASA decimals are per-asset and not in this payload
            "value_total": str(amount + close_amount),
            "value_total_decimal": None,
            "asset_id": detail.get("asset-id"),
        }

    return None


def _signature_type(signature: dict[str, Any]) -> Optional[str]:
    if "sig" in signature:
        return "ed25519"
    if "multisig" in signature:
        return "multisig"
    if "logicsig" in signature:
        return "logicsig"
    return None


def map_transaction_list(payload: dict[str, Any], *, chain_tip: Optional[int] = None) -> dict[str, Any]:
    """Transaction page → `{items, next_page_params}`, with confirmations filled if the tip is known."""
    items = []
    for tx in payload.get("transactions", []) or []:
        mapped = map_transaction(tx)
        if chain_tip is not None and mapped["block_number"] is not None:
            mapped["confirmations"] = max(0, chain_tip - mapped["block_number"])
        items.append(mapped)
    return {"items": items, "next_page_params": _next_page(payload)}


def _next_page(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    token = payload.get("next-token")
    return {"next_token": token} if token else None


# ----------------------------------------------------------------------- stats


def map_asset_holders(payload: dict[str, Any], *, decimals: Optional[int] = None) -> dict[str, Any]:
    """Indexer `/v2/assets/{id}/balances` → Blockscout-shaped holder page."""
    items = [
        {
            "address": {"hash": balance.get("address")},
            "value": str(balance.get("amount", 0)),
            "value_decimal": decimal_string(balance.get("amount", 0), decimals) if decimals is not None else None,
            "is_frozen": balance.get("is-frozen", False),
            "deleted": balance.get("deleted", False),
        }
        for balance in payload.get("balances", []) or []
    ]
    return {"items": items, "next_page_params": _next_page(payload)}


def map_stats(status: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """algod `/v2/status` + client health → Blockscout-shaped `/api/v2/stats`."""
    return {
        "total_blocks": str(status.get("last-round", 0)),
        "chain_tip": status.get("last-round"),
        "indexer_round": health.get("indexer_round"),
        "indexer_lag_rounds": health.get("indexer_lag_rounds"),
        "average_block_time": status.get("time-since-last-round"),
        "network": health.get("network"),
        "is_evm": False,
        "native_currency": {"symbol": "ALGO", "decimals": ALGO_DECIMALS},
        # No mempool-pricing concept, no gas market:
        "gas_prices": None,
        "gas_used_today": None,
    }
