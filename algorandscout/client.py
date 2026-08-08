# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Async Algorand read client — algod + indexer, no SDK dependency.

Two upstreams, deliberately kept distinct because they answer different questions:

* **algod** (`ALGORAND_ALGOD_URL`) is the node. It knows *now*: current round, live
  account state, suggested params. It does not know history.
* **indexer** (`ALGORAND_INDEXER_URL`) is the archive. It knows *history*: transactions
  by address, blocks by round, assets, applications. It lags the node by a round or two.

Neither is trusted to be up. Every call retries 5xx three times with jittered backoff and
never retries 4xx — except 429, which describes when a request arrived rather than what
was in it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_ALGOD = "https://mainnet-api.algonode.cloud"
DEFAULT_INDEXER = "https://mainnet-idx.algonode.cloud"

#: Public AlgoNode endpoints per network. Defaults are **derived from the network**, never
#: fixed to mainnet: setting `ALGORAND_NETWORK=testnet` and forgetting the URLs must not
#: silently serve mainnet data under a testnet label. Reporting the wrong chain's state as
#: the right chain's is the exact failure this project exists to prevent.
NETWORK_ENDPOINTS: dict[str, tuple[str, str]] = {
    "mainnet": ("https://mainnet-api.algonode.cloud", "https://mainnet-idx.algonode.cloud"),
    "testnet": ("https://testnet-api.algonode.cloud", "https://testnet-idx.algonode.cloud"),
    "betanet": ("https://betanet-api.algonode.cloud", "https://betanet-idx.algonode.cloud"),
    "localnet": ("http://localhost:4001", "http://localhost:8980"),
}

#: Public AlgoNode endpoints are keyless. Other providers (Nodely, Purestake-style
#: gateways, a self-hosted node) want a token header; the header *name* differs by
#: provider, so it is configurable rather than hardcoded.
DEFAULT_TOKEN_HEADER = "X-Algo-API-Token"

MAX_RETRIES = 3
RETRY_BACKOFF_S = 0.4
RETRY_JITTER_S = 0.25
#: Cap on how long an upstream `Retry-After` may park a request. A cooperative
#: provider sends seconds; a hostile or misconfigured one can send hours, and a
#: request that sleeps for hours is indistinguishable from a hang.
MAX_RETRY_AFTER_S = 10.0


def _parse_retry_after(value: str | None) -> float | None:
    """
    Parse a `Retry-After` header, clamped.

    Only the delta-seconds form is honoured; the HTTP-date form is ignored rather
    than guessed at, because a wrong clock skew turns a 2-second pause into a
    2-hour one.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


class AlgorandError(RuntimeError):
    """Upstream returned an error. `status` is the HTTP status, or None on transport failure."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url

    @property
    def retryable(self) -> bool:
        """
        4xx is deterministic and must not be retried — with one exception.

        429 is not a statement about the request, it is a statement about *when*
        the request arrived. Retrying after a backoff is the documented remedy,
        and treating it as permanent means the service fails hard under exactly
        the load it should be riding out.
        """
        return self.status is None or self.status >= 500 or self.status == 429

    @property
    def caller_error(self) -> bool:
        """True when the upstream blamed the request, not itself (4xx except 429)."""
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


class NotFound(AlgorandError):
    """The resource does not exist on this network (404). Never retried."""


@dataclass
class AlgorandConfig:
    algod_url: str = ""
    indexer_url: str = ""
    api_token: str = field(default_factory=lambda: os.environ.get("ALGORAND_API_TOKEN", ""))
    token_header: str = field(default_factory=lambda: os.environ.get("ALGORAND_API_TOKEN_HEADER", DEFAULT_TOKEN_HEADER))
    network: str = field(default_factory=lambda: os.environ.get("ALGORAND_NETWORK", "mainnet").lower())
    timeout_s: float = field(default_factory=lambda: float(os.environ.get("ALGORAND_TIMEOUT_S", "30")))
    user_agent: str = field(default_factory=lambda: os.environ.get("ALGORAND_USER_AGENT", "Algorandscout/1.0.0"))

    def __post_init__(self) -> None:
        if self.network not in NETWORK_ENDPOINTS:
            raise ValueError(
                f"unknown ALGORAND_NETWORK {self.network!r}; expected one of {sorted(NETWORK_ENDPOINTS)}. "
                "Refusing to start rather than guess which chain to read."
            )

        default_algod, default_indexer = NETWORK_ENDPOINTS[self.network]
        # Precedence: explicit argument > environment > network-derived default. An explicit
        # URL always wins, because self-hosted nodes and paid providers are the normal case.
        self.algod_url = (self.algod_url or os.environ.get("ALGORAND_ALGOD_URL") or default_algod).rstrip("/")
        self.indexer_url = (self.indexer_url or os.environ.get("ALGORAND_INDEXER_URL") or default_indexer).rstrip("/")

        # A URL that names a *different* network than the label is how mainnet data ends up
        # reported as testnet. It can be legitimate (a proxy, a private node), so this warns
        # rather than raises — but it never passes silently.
        for role, url in (("algod", self.algod_url), ("indexer", self.indexer_url)):
            for other in NETWORK_ENDPOINTS:
                if other != self.network and other in url.lower():
                    log.warning(
                        "config mismatch: network=%s but %s URL mentions %r (%s). "
                        "Responses will be labelled %s — verify this is intended.",
                        self.network,
                        role,
                        other,
                        url,
                        self.network,
                    )

    @property
    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self.api_token:
            h[self.token_header] = self.api_token
        return h


class AlgorandClient:
    """
    Read-only. There is no signing path here and there must never be one — this module
    exists to *observe* Algorand, and an observer that can spend is a different, far more
    dangerous thing.
    """

    def __init__(self, config: AlgorandConfig | None = None, session: aiohttp.ClientSession | None = None):
        self.config = config or AlgorandConfig()
        self._session = session
        self._owns_session = session is None

    # PYI034: `typing.Self` would be the better annotation, but it lands in typing
    # only in 3.11 and this package supports 3.10 without pulling typing_extensions.
    async def __aenter__(self) -> AlgorandClient:  # noqa: PYI034
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    # ---------------------------------------------------------------- transport

    async def _get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{base}{path}" + (f"?{urlencode(query)}" if query else "")
        session = await self._ensure_session()

        last: AlgorandError | None = None
        retry_after: float | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.get(url, headers=self.config.headers) as resp:
                    if resp.status == 404:
                        raise NotFound(f"not found: {path}", status=404, url=url)
                    if resp.status >= 400:
                        body = (await resp.text())[:400]
                        err = AlgorandError(f"HTTP {resp.status}: {body}", status=resp.status, url=url)
                        if not err.retryable:
                            raise err
                        last = err
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    else:
                        return await resp.json(content_type=None)
            except NotFound:
                raise
            except AlgorandError as exc:
                if not exc.retryable:
                    raise
                last = exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = AlgorandError(f"transport failure: {exc}", status=None, url=url)

            if attempt < MAX_RETRIES:
                # Jittered exponential-ish backoff. Without jitter, a fleet of
                # workers that hit a 429 together retries together, reproducing
                # the burst that caused it.
                delay = (
                    retry_after
                    if retry_after is not None
                    else (RETRY_BACKOFF_S * attempt + random.uniform(0, RETRY_JITTER_S))
                )
                log.warning("algorand upstream retry %d/%d in %.2fs: %s", attempt, MAX_RETRIES, delay, last)
                await asyncio.sleep(delay)
                retry_after = None

        raise last or AlgorandError("unknown upstream failure", url=url)

    async def algod(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self._get(self.config.algod_url, path, params)

    async def indexer(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self._get(self.config.indexer_url, path, params)

    # ------------------------------------------------------------------ reads

    async def status(self) -> dict[str, Any]:
        """Node status. `last-round` is the chain tip."""
        return await self.algod("/v2/status")

    async def health(self) -> dict[str, Any]:
        """Both upstreams, and how far the archive trails the node."""
        node: dict[str, Any] = {}
        idx: dict[str, Any] = {}
        errors: dict[str, str] = {}
        try:
            node = await self.status()
        except AlgorandError as exc:
            errors["algod"] = str(exc)
        try:
            idx = await self.indexer("/health")
        except AlgorandError as exc:
            errors["indexer"] = str(exc)

        node_round = node.get("last-round")
        idx_round = idx.get("round")
        lag = None
        note = None
        if isinstance(node_round, int) and isinstance(idx_round, int):
            lag = node_round - idx_round
            if lag < 0:
                # The two upstreams are read sequentially, not atomically. A round can be
                # produced between the calls, so the archive can appear "ahead" of the node.
                # Reported raw rather than clamped to 0 — a monitor should see the artifact
                # for what it is instead of a manufactured zero.
                note = "negative lag: node and indexer were sampled at different instants, not atomically"

        return {
            "network": self.config.network,
            "algod_url": self.config.algod_url,
            "indexer_url": self.config.indexer_url,
            "node_round": node_round,
            "indexer_round": idx_round,
            "indexer_lag_rounds": lag,
            "healthy": not errors,
            "errors": errors or None,
            "note": note,
        }

    async def account(self, address: str, *, live: bool = False) -> dict[str, Any]:
        """
        `live=True` reads the node (current state, no history).
        Default reads the indexer, which also reports `created-at-round` and `deleted`.
        """
        if live:
            return await self.algod(f"/v2/accounts/{address}")
        return await self.indexer(f"/v2/accounts/{address}")

    async def account_assets(self, address: str, *, limit: int = 50, next_token: str | None = None) -> dict[str, Any]:
        return await self.indexer(f"/v2/accounts/{address}/assets", {"limit": limit, "next": next_token})

    async def account_transactions(
        self,
        address: str,
        *,
        limit: int = 20,
        next_token: str | None = None,
        after_time: str | None = None,
        before_time: str | None = None,
        tx_type: str | None = None,
        asset_id: int | None = None,
    ) -> dict[str, Any]:
        """
        `after_time`/`before_time` are RFC-3339. These are the only endpoints with a real
        time filter, so any time-bounded question should start here rather than paginate
        another endpoint from one end of history.
        """
        return await self.indexer(
            f"/v2/accounts/{address}/transactions",
            {
                "limit": limit,
                "next": next_token,
                "after-time": after_time,
                "before-time": before_time,
                "tx-type": tx_type,
                "asset-id": asset_id,
            },
        )

    async def transaction(self, txid: str) -> dict[str, Any]:
        return await self.indexer(f"/v2/transactions/{txid}")

    async def block(self, round_number: int) -> dict[str, Any]:
        return await self.indexer(f"/v2/blocks/{round_number}")

    async def asset(self, asset_id: int) -> dict[str, Any]:
        return await self.indexer(f"/v2/assets/{asset_id}")

    async def asset_balances(self, asset_id: int, *, limit: int = 50, next_token: str | None = None) -> dict[str, Any]:
        return await self.indexer(f"/v2/assets/{asset_id}/balances", {"limit": limit, "next": next_token})

    async def application(self, app_id: int) -> dict[str, Any]:
        return await self.indexer(f"/v2/applications/{app_id}")

    async def application_boxes(self, app_id: int, *, limit: int = 50, next_token: str | None = None) -> dict[str, Any]:
        return await self.indexer(f"/v2/applications/{app_id}/boxes", {"limit": limit, "next": next_token})

    async def search_assets(
        self, *, unit: str | None = None, name: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """Symbol lookup. Returns *candidates* — ASA unit-names are not unique and never were."""
        return await self.indexer("/v2/assets", {"unit": unit, "name": name, "limit": limit})

    # -------------------------------------------------------------- pagination

    async def paginate(
        self, method: str, *args: Any, max_pages: int = 20, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Follow Algorand's `next-token` cursor. Bounded by `max_pages` on purpose: an
        unbounded follow on a hot account walks the whole chain. Callers that genuinely
        want everything must raise the bound explicitly and know what they asked for.
        """
        fn = getattr(self, method)
        token: str | None = None
        for page in range(max_pages):
            payload = await fn(*args, next_token=token, **kwargs)
            yield payload
            token = payload.get("next-token")
            if not token:
                return
        log.warning("paginate(%s) stopped at max_pages=%d with cursor still open", method, max_pages)
