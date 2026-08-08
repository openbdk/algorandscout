# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Client tests — retry policy, error classification, config, pagination.

All offline. The transport is stubbed at `_get`, so these test the *policy* (what gets
retried, what does not, when we stop) rather than aiohttp.
"""

from __future__ import annotations

import pytest

from algorandscout.client import (
    DEFAULT_ALGOD,
    DEFAULT_INDEXER,
    AlgorandClient,
    AlgorandConfig,
    AlgorandError,
    NotFound,
)


class TestConfig:
    def test_defaults_are_public_keyless_endpoints(self):
        config = AlgorandConfig()
        assert config.algod_url == DEFAULT_ALGOD
        assert config.indexer_url == DEFAULT_INDEXER

    def test_trailing_slash_stripped(self):
        config = AlgorandConfig(algod_url="https://node.example/", indexer_url="https://idx.example/")
        assert config.algod_url == "https://node.example"
        assert config.indexer_url == "https://idx.example"

    def test_no_token_header_when_keyless(self):
        assert "X-Algo-API-Token" not in AlgorandConfig(api_token="").headers

    def test_token_header_name_is_configurable(self):
        config = AlgorandConfig(api_token="secret", token_header="X-Custom-Token")
        assert config.headers["X-Custom-Token"] == "secret"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("ALGORAND_INDEXER_URL", "https://testnet-idx.example")
        monkeypatch.setenv("ALGORAND_NETWORK", "testnet")
        config = AlgorandConfig()
        assert config.indexer_url == "https://testnet-idx.example"
        assert config.network == "testnet"

    def test_user_agent_always_identifies_the_module(self):
        assert "Algorandscout" in AlgorandConfig().headers["User-Agent"]


class TestErrorClassification:
    def test_5xx_is_retryable(self):
        assert AlgorandError("boom", status=500).retryable
        assert AlgorandError("boom", status=503).retryable

    def test_4xx_is_not_retryable(self):
        assert not AlgorandError("bad request", status=400).retryable
        assert not AlgorandError("rate limited", status=429).retryable

    def test_transport_failure_is_retryable(self):
        assert AlgorandError("connection reset", status=None).retryable

    def test_not_found_is_an_algorand_error_but_never_retried(self):
        err = NotFound("missing", status=404)
        assert isinstance(err, AlgorandError)
        assert not err.retryable


class TestReadMethods:
    """Each read must hit the right upstream — history from the indexer, tip from the node."""

    @pytest.fixture
    def spy(self, monkeypatch):
        calls: list[tuple[str, str, dict]] = []

        async def fake_get(self, base, path, params=None):
            calls.append((base, path, dict(params or {})))
            return {"ok": True}

        monkeypatch.setattr(AlgorandClient, "_get", fake_get, raising=True)
        return calls

    async def test_status_uses_algod(self, spy):
        client = AlgorandClient()
        await client.status()
        base, path, _ = spy[0]
        assert base == DEFAULT_ALGOD
        assert path == "/v2/status"

    async def test_account_history_uses_indexer(self, spy):
        await AlgorandClient().account("ADDR")
        base, path, _ = spy[0]
        assert base == DEFAULT_INDEXER
        assert path == "/v2/accounts/ADDR"

    async def test_account_live_uses_algod(self, spy):
        await AlgorandClient().account("ADDR", live=True)
        assert spy[0][0] == DEFAULT_ALGOD

    async def test_time_window_params_forwarded(self, spy):
        await AlgorandClient().account_transactions(
            "ADDR", after_time="2026-01-01T00:00:00Z", before_time="2026-02-01T00:00:00Z", tx_type="axfer"
        )
        _, path, params = spy[0]
        assert path == "/v2/accounts/ADDR/transactions"
        assert params["after-time"] == "2026-01-01T00:00:00Z"
        assert params["before-time"] == "2026-02-01T00:00:00Z"
        assert params["tx-type"] == "axfer"

    async def test_none_params_are_dropped_not_sent_as_null(self, spy):
        await AlgorandClient().account_transactions("ADDR")
        _, _, params = spy[0]
        assert "next" not in params or params["next"] is None
        assert params.get("after-time") is None

    async def test_asset_and_application_paths(self, spy):
        client = AlgorandClient()
        await client.asset(31566704)
        await client.application(1002541853)
        assert spy[0][1] == "/v2/assets/31566704"
        assert spy[1][1] == "/v2/applications/1002541853"


class TestHealth:
    async def test_reports_lag_between_node_and_indexer(self, monkeypatch):
        async def fake_get(self, base, path, params=None):
            if path == "/v2/status":
                return {"last-round": 1000}
            return {"round": 997}

        monkeypatch.setattr(AlgorandClient, "_get", fake_get, raising=True)
        health = await AlgorandClient().health()
        assert health["indexer_lag_rounds"] == 3
        assert health["healthy"] is True

    async def test_degraded_upstream_reported_not_swallowed(self, monkeypatch):
        async def fake_get(self, base, path, params=None):
            if path == "/v2/status":
                raise AlgorandError("node down", status=503)
            return {"round": 997}

        monkeypatch.setattr(AlgorandClient, "_get", fake_get, raising=True)
        health = await AlgorandClient().health()
        assert health["healthy"] is False
        assert "algod" in health["errors"]
        assert health["indexer_lag_rounds"] is None


class TestPagination:
    async def test_follows_next_token_until_exhausted(self, monkeypatch):
        pages = [
            {"transactions": [1], "next-token": "a"},
            {"transactions": [2], "next-token": "b"},
            {"transactions": [3]},
        ]
        seen: list[str | None] = []

        async def fake_account_transactions(self, address, *, limit=20, next_token=None, **kwargs):
            seen.append(next_token)
            return pages[len(seen) - 1]

        monkeypatch.setattr(AlgorandClient, "account_transactions", fake_account_transactions, raising=True)

        collected = [page async for page in AlgorandClient().paginate("account_transactions", "ADDR")]
        assert len(collected) == 3
        assert seen == [None, "a", "b"]

    async def test_stops_at_max_pages(self, monkeypatch):
        async def endless(self, address, *, limit=20, next_token=None, **kwargs):
            return {"transactions": [1], "next-token": "always-more"}

        monkeypatch.setattr(AlgorandClient, "account_transactions", endless, raising=True)

        collected = [page async for page in AlgorandClient().paginate("account_transactions", "ADDR", max_pages=4)]
        assert len(collected) == 4, "an open cursor must not walk the chain forever"


class TestReadOnlyGuarantee:
    def test_client_exposes_no_write_surface(self):
        """
        The module observes. If a signing or submission method ever appears here, this
        test should fail and the reviewer should ask why.
        """
        forbidden = {"send", "submit", "sign", "post", "broadcast", "send_transaction", "submit_transaction"}
        public = {name for name in dir(AlgorandClient) if not name.startswith("_")}
        assert not (public & forbidden), f"write surface introduced: {public & forbidden}"
