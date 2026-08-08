# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
The HTTP surface — a Blockscout-shaped read API for Algorand.

Two route families, mounted side by side:

* ``/api/v2/*`` — Blockscout-shaped. A client written against a Blockscout explorer API
  keeps working, subject to the boundary declared at ``/api/v2/capabilities``.
* ``/algorand/v2/*`` — a safelisted passthrough to the native indexer, for callers who
  want Algorand's own shapes with none of the translation.

The service is **read-only end to end**. There is no signing key, no write route, and no
transaction submission. Adding one would change what this module is.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import capabilities as caps
from .client import AlgorandClient, AlgorandConfig, AlgorandError, NotFound
from .mapping import (
    map_account,
    map_account_assets,
    map_application,
    map_asset,
    map_block,
    map_stats,
    map_transaction,
    map_transaction_list,
)

log = logging.getLogger(__name__)

#: Native indexer paths the passthrough will serve. An allowlist rather than a prefix
#: match: the passthrough must not become an open proxy to anything the upstream adds later.
PASSTHROUGH_PREFIXES = (
    "/v2/accounts",
    "/v2/assets",
    "/v2/applications",
    "/v2/blocks",
    "/v2/transactions",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = AlgorandClient(AlgorandConfig())
    log.info(
        "algorandscout %s up — network=%s algod=%s indexer=%s",
        caps.MODULE_VERSION,
        app.state.client.config.network,
        app.state.client.config.algod_url,
        app.state.client.config.indexer_url,
    )
    try:
        yield
    finally:
        await app.state.client.close()


app = FastAPI(
    title="Algorandscout",
    version=caps.MODULE_VERSION,
    description=(
        "Blockscout-shaped read API for Algorand. Independent work, BANKON licensed; "
        "contains no Blockscout source code. Algorand is not an EVM chain — see "
        "/api/v2/capabilities before assuming a field exists."
    ),
    lifespan=lifespan,
)


def client(request: Request) -> AlgorandClient:
    return request.app.state.client


@app.exception_handler(AlgorandError)
async def algorand_error_handler(request: Request, exc: AlgorandError) -> JSONResponse:
    """Upstream failures surface as themselves — a 502 for a dead indexer, not a fake 200."""
    status = 404 if isinstance(exc, NotFound) else 502
    return JSONResponse(status_code=status, content={"error": str(exc), "upstream_status": exc.status})


# ---------------------------------------------------------------- meta routes


@app.get("/health", tags=["meta"])
async def health(request: Request) -> dict[str, Any]:
    return await client(request).health()


@app.get("/api/v2/capabilities", tags=["meta"])
async def get_capabilities() -> dict[str, Any]:
    """What this module can and cannot answer, and why. Read this before building a query."""
    return caps.capabilities()


@app.get("/api/v2/stats", tags=["blockscout"])
async def stats(request: Request) -> dict[str, Any]:
    c = client(request)
    status = await c.status()
    health_payload = await c.health()
    return map_stats(status, health_payload)


# ------------------------------------------------------------- address routes


@app.get("/api/v2/addresses/{address}", tags=["blockscout"])
async def get_address(address: str, request: Request, live: bool = Query(False)) -> dict[str, Any]:
    payload = await client(request).account(address, live=live)
    return map_account(payload)


@app.get("/api/v2/addresses/{address}/transactions", tags=["blockscout"])
async def get_address_transactions(
    address: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    next_token: Optional[str] = Query(None),
    after_time: Optional[str] = Query(None, description="RFC-3339; the Algorand analogue of age_from"),
    before_time: Optional[str] = Query(None, description="RFC-3339; the Algorand analogue of age_to"),
    tx_type: Optional[str] = Query(None, description="pay | axfer | acfg | afrz | appl | keyreg | stpf"),
    asset_id: Optional[int] = Query(None),
) -> dict[str, Any]:
    c = client(request)
    payload = await c.account_transactions(
        address,
        limit=limit,
        next_token=next_token,
        after_time=after_time,
        before_time=before_time,
        tx_type=tx_type,
        asset_id=asset_id,
    )
    tip = payload.get("current-round")
    return map_transaction_list(payload, chain_tip=tip)


@app.get("/api/v2/addresses/{address}/token-balances", tags=["blockscout"])
async def get_address_token_balances(
    address: str,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    next_token: Optional[str] = Query(None),
    resolve: bool = Query(True, description="Resolve ASA metadata (name/decimals) per holding"),
) -> dict[str, Any]:
    """
    Holdings only. The native ALGO balance is on `/api/v2/addresses/{address}` and is a
    *separate* surface — an answer built from this endpoint alone omits what is usually
    the largest position.
    """
    c = client(request)
    payload = await c.account_assets(address, limit=limit, next_token=next_token)

    params: dict[int, dict] = {}
    if resolve:
        for holding in payload.get("assets", []) or []:
            asset_id = holding.get("asset-id")
            if asset_id is None or asset_id in params:
                continue
            try:
                asset_payload = await c.asset(asset_id)
                params[asset_id] = (asset_payload.get("asset", {}) or {}).get("params", {}) or {}
            except AlgorandError as exc:  # a destroyed ASA 404s; the holding is still real
                log.debug("asset %s unresolved: %s", asset_id, exc)

    return map_account_assets(payload, params)


# ---------------------------------------------------------- transaction routes


@app.get("/api/v2/transactions/{txid}", tags=["blockscout"])
async def get_transaction(txid: str, request: Request) -> dict[str, Any]:
    payload = await client(request).transaction(txid)
    tx = payload.get("transaction")
    if not tx:
        raise HTTPException(status_code=404, detail="transaction not found")
    mapped = map_transaction(tx)
    tip = payload.get("current-round")
    if tip and mapped["block_number"]:
        mapped["confirmations"] = max(0, tip - mapped["block_number"])
    return mapped


# ---------------------------------------------------------------- block routes


@app.get("/api/v2/blocks/{round_number}", tags=["blockscout"])
async def get_block(
    round_number: int,
    request: Request,
    include_transactions: bool = Query(False, description="Off by default; a busy round is large"),
) -> dict[str, Any]:
    payload = await client(request).block(round_number)
    return map_block(payload, include_transactions=include_transactions)


@app.get("/api/v2/blocks", tags=["blockscout"])
async def get_latest_block(request: Request) -> dict[str, Any]:
    c = client(request)
    status = await c.status()
    tip = status.get("last-round")
    if tip is None:
        raise HTTPException(status_code=502, detail="node did not report a round")
    payload = await c.block(tip)
    return {"items": [map_block(payload)], "next_page_params": None}


# ---------------------------------------------------------------- token routes


@app.get("/api/v2/tokens/{asset_id}", tags=["blockscout"])
async def get_token(asset_id: int, request: Request) -> dict[str, Any]:
    payload = await client(request).asset(asset_id)
    return map_asset(payload)


@app.get("/api/v2/tokens/{asset_id}/holders", tags=["blockscout"])
async def get_token_holders(
    asset_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    next_token: Optional[str] = Query(None),
) -> dict[str, Any]:
    payload = await client(request).asset_balances(asset_id, limit=limit, next_token=next_token)
    items = [
        {
            "address": {"hash": b.get("address")},
            "value": str(b.get("amount", 0)),
            "is_frozen": b.get("is-frozen", False),
        }
        for b in payload.get("balances", []) or []
    ]
    return {"items": items, "next_page_params": {"next_token": payload["next-token"]} if payload.get("next-token") else None}


# ------------------------------------------------------------- contract routes


@app.get("/api/v2/smart-contracts/{app_id}", tags=["blockscout"])
async def get_smart_contract(app_id: int, request: Request) -> dict[str, Any]:
    payload = await client(request).application(app_id)
    return map_application(payload)


# ----------------------------------------------------------------------- search


@app.get("/api/v2/search", tags=["blockscout"])
async def search(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """
    Resolves by shape: 58 chars → address, 52 chars → transaction id, all digits →
    asset **and** application id (both are integers and the namespaces overlap, so both
    are probed and both are returned when both hit).

    Anything else is treated as an ASA unit-name or name search, which returns
    *candidates*. ASA names are not unique; nothing here ranks them, because any ranking
    would imply an authenticity judgment this module is not entitled to make.
    """
    c = client(request)
    results: list[dict[str, Any]] = []
    query = q.strip()

    if len(query) == 58 and query.isalnum() and query.isupper():
        try:
            results.append({"type": "address", "data": map_account(await c.account(query))})
        except AlgorandError:
            pass
    elif len(query) == 52 and query.isalnum() and query.isupper():
        try:
            payload = await c.transaction(query)
            if payload.get("transaction"):
                results.append({"type": "transaction", "data": map_transaction(payload["transaction"])})
        except AlgorandError:
            pass
    elif query.isdigit():
        numeric = int(query)
        for kind, fetch, mapper in (
            ("token", c.asset, map_asset),
            ("smart_contract", c.application, map_application),
        ):
            try:
                results.append({"type": kind, "data": mapper(await fetch(numeric))})
            except AlgorandError:
                pass
    else:
        payload = await c.search_assets(unit=query, limit=limit)
        results.extend({"type": "token", "data": map_asset({"asset": a})} for a in payload.get("assets", []) or [])
        if not results:
            payload = await c.search_assets(name=query, limit=limit)
            results.extend({"type": "token", "data": map_asset({"asset": a})} for a in payload.get("assets", []) or [])

    return {
        "items": results,
        "note": "Asset name/unit matches are candidates, not identities. Resolve by asset ID.",
    }


# ------------------------------------------------------------ native passthrough


@app.get("/algorand/v2/{path:path}", tags=["native"])
async def passthrough(path: str, request: Request) -> Any:
    """Safelisted native indexer passthrough — Algorand's own shapes, untranslated."""
    target = f"/v2/{path}"
    if not any(target.startswith(prefix) for prefix in PASSTHROUGH_PREFIXES):
        raise HTTPException(status_code=403, detail=f"path not in passthrough allowlist: {target}")
    params = dict(request.query_params)
    return await client(request).indexer(target, params)
