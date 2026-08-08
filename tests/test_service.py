# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Route-level tests. The upstream client is replaced with a stub, so these exercise the HTTP
surface — status codes, wiring, error translation, allowlisting — without a network.

The suite previously covered the client and the mapper but never the routes that join them,
which is where several of the audit findings actually lived.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from algorandscout.client import AlgorandConfig, AlgorandError, NotFound
from algorandscout.service import app

FIXTURES = Path(__file__).parent / "fixtures"

#: A real, checksum-valid mainnet address. "ADDR" no longer reaches the routes:
#: identifiers are validated at the boundary now, which is the point.
ADDR = "2UEQTE5QDNXPI7M3TU44G6SYKLFWLPQO7EBZM7K7MHMQQMFI4QJPLHQFHM"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class StubClient:
    """Records calls and returns fixtures. Raises whatever `fail_with` is set to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.fail_with: Exception | None = None
        self.asset_calls = 0
        # The lifespan hook logs the resolved endpoints on startup, so the stub has to
        # carry a config the same way the real client does.
        self.config = AlgorandConfig(network="mainnet")

    def _record(self, name: str, *args):
        self.calls.append((name, args))
        if self.fail_with:
            raise self.fail_with

    async def status(self):
        self._record("status")
        return {"last-round": 63_879_061, "time-since-last-round": 2_800_000_000}

    async def health(self):
        self._record("health")
        return {"network": "mainnet", "indexer_round": 63_879_058, "indexer_lag_rounds": 3, "healthy": True}

    async def account(self, address, *, live=False):
        self._record("account", address, live)
        return fixture("account.json")

    async def account_assets(self, address, *, limit=50, next_token=None):
        self._record("account_assets", address)
        return {"assets": [{"asset-id": 31566704, "amount": 1_500_000, "is-frozen": False}]}

    async def account_transactions(self, address, **kwargs):
        self._record("account_transactions", address, tuple(sorted(kwargs.items())))
        return fixture("transactions.json")

    async def transaction(self, txid):
        self._record("transaction", txid)
        return {"transaction": fixture("transactions.json")["transactions"][0], "current-round": 63_879_061}

    async def block(self, round_number):
        self._record("block", round_number)
        return fixture("block.json")

    async def asset(self, asset_id):
        self.asset_calls += 1
        self._record("asset", asset_id)
        return fixture("asset_usdc.json")

    async def asset_balances(self, asset_id, *, limit=50, next_token=None):
        self._record("asset_balances", asset_id)
        return {"balances": [{"address": "HOLDER", "amount": 2_000_000, "is-frozen": False}]}

    async def application(self, app_id):
        self._record("application", app_id)
        return fixture("application.json")

    async def search_assets(self, *, unit=None, name=None, limit=10):
        self._record("search_assets", unit, name)
        return {"assets": [fixture("asset_usdc.json")["asset"]]}

    async def indexer(self, path, params=None):
        self._record("indexer", path)
        return {"passthrough": path}

    async def close(self):
        pass


@pytest.fixture
def stub(monkeypatch):
    client = StubClient()

    from algorandscout import service

    monkeypatch.setattr(service, "AlgorandClient", lambda *a, **k: client)
    return client


@pytest.fixture
def api(stub):
    with TestClient(app) as test_client:
        yield test_client, stub


class TestMetaRoutes:
    def test_capabilities_declares_non_evm(self, api):
        client, _ = api
        body = client.get("/api/v2/capabilities").json()
        assert body["chain"]["is_evm"] is False
        assert body["module"] == "algorandscout"
        assert "logs_by_topic" in body["unsupported"]

    def test_health(self, api):
        client, _ = api
        assert client.get("/health").json()["healthy"] is True

    def test_stats(self, api):
        client, _ = api
        body = client.get("/api/v2/stats").json()
        assert body["chain_tip"] == 63_879_061
        assert body["is_evm"] is False
        assert body["gas_prices"] is None


class TestAddressRoutes:
    def test_address(self, api):
        client, _ = api
        body = client.get("/api/v2/addresses/" + ADDR + "").json()
        assert body["coin_balance_decimal"] == "5401.728855"
        assert body["nonce"] is None

    def test_live_flag_reaches_the_client(self, api):
        client, stub = api
        client.get("/api/v2/addresses/" + ADDR + "?live=true")
        assert ("account", (ADDR, True)) in stub.calls

    def test_transactions_carry_confirmations(self, api):
        client, _ = api
        body = client.get("/api/v2/addresses/" + ADDR + "/transactions").json()
        assert body["items"][0]["confirmations"] is not None

    def test_time_window_forwarded(self, api):
        client, stub = api
        client.get("/api/v2/addresses/" + ADDR + "/transactions?after_time=2026-01-01T00:00:00Z&tx_type=axfer")
        kwargs = dict(next(c[1][1] for c in stub.calls if c[0] == "account_transactions"))
        assert kwargs["after_time"] == "2026-01-01T00:00:00Z"
        assert kwargs["tx_type"] == "axfer"

    def test_limit_is_bounded(self, api):
        client, _ = api
        assert client.get("/api/v2/addresses/" + ADDR + "/transactions?limit=101").status_code == 422

    def test_token_balances_resolve_metadata(self, api):
        client, _ = api
        item = client.get("/api/v2/addresses/" + ADDR + "/token-balances").json()["items"][0]
        assert item["token"]["symbol"] == "USDC"
        assert item["value_decimal"] == "1.5"

    def test_resolve_false_skips_upstream_lookups(self, api):
        client, stub = api
        client.get("/api/v2/addresses/" + ADDR + "/token-balances?resolve=false")
        assert stub.asset_calls == 0


class TestTokenAndContractRoutes:
    def test_token(self, api):
        client, _ = api
        body = client.get("/api/v2/tokens/31566704").json()
        assert body["symbol"] == "USDC"
        assert body["clawback_enabled"] is False

    def test_holders_render_with_decimals(self, api):
        client, _ = api
        item = client.get("/api/v2/tokens/31566704/holders").json()["items"][0]
        assert item["value"] == "2000000"
        assert item["value_decimal"] == "2"

    def test_smart_contract_claims_no_abi(self, api):
        client, _ = api
        body = client.get("/api/v2/smart-contracts/1002541853").json()
        assert body["language"] == "TEAL/AVM"
        assert body["abi"] is None
        assert body["is_verified"] is False


class TestBlockRoutes:
    def test_block_omits_own_hash(self, api):
        client, _ = api
        body = client.get("/api/v2/blocks/63879000").json()
        assert body["height"] == 63_879_000
        assert body["hash"] is None
        assert body["gas_used"] is None

    def test_latest_block_uses_the_tip(self, api):
        client, stub = api
        client.get("/api/v2/blocks")
        assert ("block", (63_879_061,)) in stub.calls


class TestSearch:
    def test_numeric_query_probes_both_namespaces(self, api):
        client, stub = api
        body = client.get("/api/v2/search?q=31566704").json()
        kinds = {item["type"] for item in body["items"]}
        assert "token" in kinds
        assert any(c[0] == "application" for c in stub.calls), "app-id namespace must also be probed"

    def test_candidates_are_labelled_as_such(self, api):
        client, _ = api
        assert "candidates" in client.get("/api/v2/search?q=USDC").json()["note"]

    def test_empty_query_rejected(self, api):
        client, _ = api
        assert client.get("/api/v2/search?q=").status_code == 422


class TestPassthrough:
    def test_allowlisted_path_served(self, api):
        client, _ = api
        assert client.get("/algorand/v2/assets/31566704").status_code == 200

    def test_unlisted_path_forbidden(self, api):
        client, _ = api
        assert client.get("/algorand/v2/status").status_code == 403

    def test_lookalike_prefix_forbidden(self, api):
        """`/v2/accountsX` must not ride in on the `/v2/accounts` allowlist entry."""
        client, _ = api
        assert client.get("/algorand/v2/accountsX").status_code == 403


class TestErrorTranslation:
    def test_upstream_404_becomes_404(self, api):
        client, stub = api
        stub.fail_with = NotFound("no such asset", status=404)
        assert client.get("/api/v2/tokens/1").status_code == 404

    def test_upstream_5xx_becomes_502_not_a_fake_200(self, api):
        client, stub = api
        stub.fail_with = AlgorandError("indexer down", status=503)
        response = client.get("/api/v2/tokens/1")
        assert response.status_code == 502
        assert response.json()["upstream_status"] == 503
