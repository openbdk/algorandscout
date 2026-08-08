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

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import capabilities as caps
from .cache import TTLCache
from .client import AlgorandClient, AlgorandConfig, AlgorandError, NotFound
from .metrics import METRICS, route_label
from .validation import (
    ValidationError,
    classify_query,
    validate_address,
    validate_txid,
    validate_uint64,
)
from .mapping import (
    map_account,
    map_account_assets,
    map_application,
    map_asset,
    map_asset_holders,
    map_block,
    map_stats,
    map_transaction,
    map_transaction_list,
)

log = logging.getLogger(__name__)

#: Max concurrent ASA metadata lookups when resolving a holdings page. Bounded so a wide
#: account cannot burst a public keyless endpoint into rate-limiting us.
RESOLVE_CONCURRENCY = 8

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
    app.state.cache = TTLCache()
    app.state.started_at = time.time()
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


def cache(request: Request) -> TTLCache:
    return request.app.state.cache


def _route_for(request: Request) -> str:
    """
    The matched route *template*, for use as a metric label.

    Read from the routing table rather than inferred from the path. A heuristic
    misses anything it did not anticipate — `/api/v2/tokens/-5` is not `.isdigit()`,
    so it survived as its own label and let a caller mint unbounded time series
    just by walking negative ids. Unmatched paths collapse to a single bucket for
    the same reason: 404-probing must not be able to grow the metric space.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    return "<unmatched>"


@app.middleware("http")
async def observability(request: Request, call_next):
    """
    Correlate and measure every request.

    The request id is echoed back so a caller reporting "it was slow at 14:03"
    can be matched to a log line without guessing.
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        METRICS.observe_request(_route_for(request), 500, time.perf_counter() - started)
        log.exception("unhandled error [%s] %s %s", request_id, request.method, request.url.path)
        raise
    duration = time.perf_counter() - started
    METRICS.observe_request(_route_for(request), status, duration)
    response.headers["X-Request-ID"] = request_id
    log.info(
        "%s %s %s %.3fs [%s]", request.method, request.url.path, status, duration, request_id
    )
    return response


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    """A malformed identifier is the caller's problem: 400, and never an upstream call."""
    return JSONResponse(status_code=400, content={"error": str(exc), "kind": "validation"})


@app.exception_handler(AlgorandError)
async def algorand_error_handler(request: Request, exc: AlgorandError) -> JSONResponse:
    """
    Attribute the failure honestly.

    A 502 means "the upstream is broken" and pages someone. Returning it for a
    request the upstream rejected as malformed misdirects that page and burns the
    upstream error budget on client mistakes — so a caller error stays a 4xx.
    """
    if isinstance(exc, NotFound):
        status = 404
    elif exc.status == 429:
        status = 429  # pass the backpressure through rather than masking it as 502
    elif exc.caller_error:
        status = 400
    else:
        status = 502
    METRICS.observe_upstream("indexer", "caller_error" if status < 500 else "upstream_error")
    return JSONResponse(status_code=status, content={"error": str(exc), "upstream_status": exc.status})


# ---------------------------------------------------------------- meta routes


@app.get("/health", tags=["meta"])
async def health(request: Request) -> dict[str, Any]:
    return await client(request).health()


@app.get("/livez", tags=["meta"])
async def livez() -> dict[str, str]:
    """
    Liveness: is this process able to serve at all.

    Deliberately independent of the upstream. If the indexer is down, this
    service is still alive and correctly reporting that fact — restarting it
    would fix nothing, so a liveness probe must not fail on someone else's outage.
    """
    return {"status": "alive"}


@app.get("/readyz", tags=["meta"])
async def readyz(request: Request) -> JSONResponse:
    """
    Readiness: can this process actually answer questions right now.

    Unlike liveness, this DOES depend on the upstream — an instance that cannot
    reach the indexer should be taken out of the load-balancer rotation, not killed.
    """
    health_payload = await client(request).health()
    ready = bool(health_payload.get("healthy"))
    return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, **health_payload})


@app.get("/metrics", tags=["meta"], response_class=PlainTextResponse)
async def metrics(request: Request) -> str:
    """Prometheus exposition. No auth: it carries counters, never chain data or secrets."""
    uptime = time.time() - getattr(request.app.state, "started_at", time.time())
    return METRICS.render(
        cache_stats=cache(request).stats,
        extra={"uptime_seconds": round(uptime, 1), "cache_entries": len(cache(request))},
    )


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
    validate_address(address)
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
    validate_address(address)
    if asset_id is not None:
        validate_uint64(asset_id, name="asset_id")
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
    validate_address(address)
    c = client(request)
    payload = await c.account_assets(address, limit=limit, next_token=next_token)

    params: dict[int, dict] = {}
    if resolve:
        # Resolution is one upstream call per distinct ASA. Serially that is `limit` round
        # trips before the first byte reaches the caller; a 100-asset account would sit on
        # ~100 sequential requests. Fan out with a bounded semaphore instead — bounded because
        # an unbounded fan-out on a public keyless endpoint is a good way to get rate-limited.
        asset_ids = {h["asset-id"] for h in (payload.get("assets") or []) if h.get("asset-id") is not None}
        semaphore = asyncio.Semaphore(RESOLVE_CONCURRENCY)

        async def resolve_one(asset_id: int) -> tuple[int, Optional[dict]]:
            async with semaphore:
                try:
                    asset_payload = await c.asset(asset_id)
                    return asset_id, (asset_payload.get("asset", {}) or {}).get("params", {}) or {}
                except AlgorandError as exc:  # a destroyed ASA 404s; the holding is still real
                    log.debug("asset %s unresolved: %s", asset_id, exc)
                    return asset_id, None

        for asset_id, resolved in await asyncio.gather(*(resolve_one(a) for a in asset_ids)):
            if resolved is not None:
                params[asset_id] = resolved

    return map_account_assets(payload, params)


# ---------------------------------------------------------- transaction routes


@app.get("/api/v2/transactions/{txid}", tags=["blockscout"])
async def get_transaction(txid: str, request: Request) -> dict[str, Any]:
    validate_txid(txid)
    payload = await cache(request).get_or_fetch(
        # A confirmed transaction is final on Algorand — there is no reorg that
        # could change it, so it is safe to cache for as long as we like.
        "transaction", txid, lambda: client(request).transaction(txid)
    )
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
    validate_uint64(round_number, name="round")
    payload = await cache(request).get_or_fetch(
        "block", str(round_number), lambda: client(request).block(round_number)
    )
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
    validate_uint64(asset_id, name="asset_id")
    payload = await cache(request).get_or_fetch(
        "asset", str(asset_id), lambda: client(request).asset(asset_id)
    )
    return map_asset(payload)


@app.get("/api/v2/tokens/{asset_id}/holders", tags=["blockscout"])
async def get_token_holders(
    asset_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    next_token: Optional[str] = Query(None),
    resolve: bool = Query(True, description="Resolve the ASA's decimals so holdings render as decimals"),
) -> dict[str, Any]:
    validate_uint64(asset_id, name="asset_id")
    c = client(request)
    payload = await c.asset_balances(asset_id, limit=limit, next_token=next_token)
    decimals = None
    if resolve:
        try:
            decimals = ((await c.asset(asset_id)).get("asset", {}) or {}).get("params", {}).get("decimals")
        except AlgorandError as exc:
            log.debug("asset %s decimals unresolved: %s", asset_id, exc)
    return map_asset_holders(payload, decimals=decimals)


# ------------------------------------------------------------- contract routes


@app.get("/api/v2/smart-contracts/{app_id}", tags=["blockscout"])
async def get_smart_contract(app_id: int, request: Request) -> dict[str, Any]:
    validate_uint64(app_id, name="app_id")
    payload = await cache(request).get_or_fetch(
        "application", str(app_id), lambda: client(request).application(app_id)
    )
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

    kind = classify_query(query)

    if kind == "address":
        try:
            results.append({"type": "address", "data": map_account(await c.account(query))})
        except AlgorandError:
            pass
    elif kind == "transaction":
        try:
            payload = await c.transaction(query)
            if payload.get("transaction"):
                results.append({"type": "transaction", "data": map_transaction(payload["transaction"])})
        except AlgorandError:
            pass
    elif kind == "numeric":
        numeric = int(query)
        try:
            validate_uint64(numeric, name="id")
        except ValidationError:
            # Out of uint64 range cannot name anything on chain; fall through to
            # returning no results rather than asking the upstream about it.
            return {"items": [], "note": "numeric term exceeds uint64 range; cannot match an asset or app id."}
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
    # Segment-aware: a bare startswith would let "/v2/accountsX" through on the "/v2/accounts"
    # prefix. The allowlisted prefix must be followed by a path separator or end the path.
    if not any(target == p or target.startswith(p + "/") for p in PASSTHROUGH_PREFIXES):
        raise HTTPException(status_code=403, detail=f"path not in passthrough allowlist: {target}")
    params = dict(request.query_params)
    return await client(request).indexer(target, params)
