# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Production-readiness tests: validation, retry semantics, error attribution,
caching, metrics, and the probe split.

The theme is blame. A service that cannot tell "you sent nonsense" from "my
upstream fell over" wakes the wrong person up at 3am, and no amount of correct
chain mapping compensates for that.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from algorandscout.cache import TTL_SECONDS, TTLCache
from algorandscout.client import MAX_RETRY_AFTER_S, AlgorandError, NotFound, _parse_retry_after
from algorandscout.metrics import Metrics, route_label
from algorandscout.service import app
from algorandscout.validation import (
    ValidationError,
    classify_query,
    validate_address,
    validate_txid,
    validate_uint64,
)

REAL_ADDRESS = "2UEQTE5QDNXPI7M3TU44G6SYKLFWLPQO7EBZM7K7MHMQQMFI4QJPLHQFHM"
REAL_TXID = "UTXPSO7OSSQN6NFFI6QESOSMUGRMUMO7HVBMRTBTL6RLPFMUFWVQ"


# ------------------------------------------------------------------ validation


class TestAddressValidation:
    def test_real_address_accepted(self):
        assert validate_address(REAL_ADDRESS) == REAL_ADDRESS

    def test_checksum_catches_a_single_character_typo(self):
        """Addresses carry their own checksum; a typo is detectable with certainty."""
        typo = REAL_ADDRESS[:-1] + ("A" if REAL_ADDRESS[-1] != "A" else "B")
        with pytest.raises(ValidationError, match="checksum"):
            validate_address(typo)

    def test_transposition_caught(self):
        transposed = REAL_ADDRESS[:10] + REAL_ADDRESS[11] + REAL_ADDRESS[10] + REAL_ADDRESS[12:]
        with pytest.raises(ValidationError):
            validate_address(transposed)

    @pytest.mark.parametrize("bad_char", ["0", "1", "8", "9"])
    def test_non_base32_digits_rejected(self, bad_char):
        """0, 1, 8 and 9 are not in the RFC-4648 base32 alphabet."""
        with pytest.raises(ValidationError, match="base32"):
            validate_address(bad_char + REAL_ADDRESS[1:])

    def test_wrong_length_rejected(self):
        with pytest.raises(ValidationError, match="58 characters"):
            validate_address(REAL_ADDRESS[:-1])

    def test_empty_and_non_string_rejected(self):
        for value in ("", None, 12345):
            with pytest.raises(ValidationError):
                validate_address(value)  # type: ignore[arg-type]


class TestTxidValidation:
    def test_real_txid_accepted(self):
        assert validate_txid(REAL_TXID) == REAL_TXID

    def test_wrong_length_rejected(self):
        with pytest.raises(ValidationError, match="52 characters"):
            validate_txid(REAL_TXID + "A")

    def test_lowercase_rejected(self):
        with pytest.raises(ValidationError, match="base32"):
            validate_txid(REAL_TXID.lower())


class TestUint64Validation:
    def test_accepts_zero_and_max(self):
        assert validate_uint64(0) == 0
        assert validate_uint64(2**64 - 1) == 2**64 - 1

    def test_rejects_negative(self):
        with pytest.raises(ValidationError, match="negative"):
            validate_uint64(-1, name="asset_id")

    def test_rejects_overflow(self):
        with pytest.raises(ValidationError, match="uint64"):
            validate_uint64(2**64)

    def test_rejects_bool_masquerading_as_int(self):
        with pytest.raises(ValidationError):
            validate_uint64(True)


class TestQueryClassifier:
    def test_shapes(self):
        assert classify_query(REAL_ADDRESS) == "address"
        assert classify_query(REAL_TXID) == "transaction"
        assert classify_query("31566704") == "numeric"
        assert classify_query("USDC") is None
        assert classify_query("") is None


# --------------------------------------------------------------- retry policy


class TestRetrySemantics:
    def test_429_is_retryable_because_it_is_temporal(self):
        """
        429 is the one 4xx that must be retried: it describes *when* the request
        arrived, not what was in it. Treating it as permanent makes the service
        fail hardest under exactly the load it should ride out.
        """
        assert AlgorandError("rate limited", status=429).retryable

    def test_other_4xx_still_not_retried(self):
        for status in (400, 404, 422):
            assert not AlgorandError("bad", status=status).retryable

    def test_5xx_and_transport_retried(self):
        assert AlgorandError("boom", status=503).retryable
        assert AlgorandError("reset", status=None).retryable

    def test_caller_error_excludes_429_and_5xx(self):
        assert AlgorandError("bad", status=400).caller_error
        assert not AlgorandError("rate", status=429).caller_error
        assert not AlgorandError("boom", status=500).caller_error
        assert not AlgorandError("reset", status=None).caller_error

    def test_not_found_is_a_caller_error(self):
        assert NotFound("gone", status=404).caller_error


class TestRetryAfter:
    def test_delta_seconds_honoured(self):
        assert _parse_retry_after("2") == 2.0

    def test_clamped_so_a_request_cannot_sleep_for_hours(self):
        assert _parse_retry_after("86400") == MAX_RETRY_AFTER_S

    def test_http_date_form_ignored_rather_than_guessed(self):
        assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None

    def test_garbage_and_missing(self):
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("-5") is None


# --------------------------------------------------------------------- cache


class TestCache:
    def test_hit_and_miss_accounting(self):
        c = TTLCache()
        assert c.get("asset", "1") is None
        c.set("asset", "1", {"x": 1})
        assert c.get("asset", "1") == {"x": 1}
        assert c.stats.hits == 1 and c.stats.misses == 1

    def test_uncacheable_kinds_are_never_stored(self):
        """Accounts change every round; caching one would serve a stale balance."""
        c = TTLCache()
        c.set("account", "ADDR", {"amount": 1})
        assert c.get("account", "ADDR") is None
        assert "account" not in TTL_SECONDS

    def test_expiry(self, monkeypatch):
        c = TTLCache()
        now = [1000.0]
        monkeypatch.setattr(TTLCache, "_now", staticmethod(lambda: now[0]))
        c.set("asset", "1", "v")
        now[0] += TTL_SECONDS["asset"] + 1
        assert c.get("asset", "1") is None
        assert c.stats.expirations == 1

    def test_lru_eviction_is_bounded(self):
        c = TTLCache(max_entries=3)
        for i in range(5):
            c.set("asset", str(i), i)
        assert len(c) == 3
        assert c.stats.evictions == 2

    def test_settled_data_gets_the_long_ttl(self):
        """Blocks and transactions are final on Algorand — no reorg can invalidate them."""
        assert TTL_SECONDS["block"] >= TTL_SECONDS["asset"]
        assert TTL_SECONDS["transaction"] >= TTL_SECONDS["asset"]

    async def test_get_or_fetch_calls_upstream_once(self):
        c = TTLCache()
        calls = []

        async def fetch():
            calls.append(1)
            return {"v": 1}

        assert await c.get_or_fetch("asset", "1", fetch) == {"v": 1}
        assert await c.get_or_fetch("asset", "1", fetch) == {"v": 1}
        assert len(calls) == 1


# ------------------------------------------------------------------- metrics


class TestMetrics:
    def test_route_label_collapses_ids_to_avoid_cardinality_blowup(self):
        """Unbounded label cardinality is how a /metrics endpoint kills Prometheus."""
        assert route_label(f"/api/v2/addresses/{REAL_ADDRESS}") == "/api/v2/addresses/{hash}"
        assert route_label("/api/v2/tokens/31566704") == "/api/v2/tokens/{id}"
        assert route_label(f"/api/v2/transactions/{REAL_TXID}") == "/api/v2/transactions/{hash}"

    def test_exposition_is_well_formed(self):
        m = Metrics()
        m.observe_request("/api/v2/stats", 200, 0.12)
        m.observe_upstream("indexer", "ok")
        text = m.render()
        assert 'algorandscout_requests_total{route="/api/v2/stats",status="200"} 1' in text
        assert "# TYPE algorandscout_request_duration_seconds histogram" in text
        assert text.endswith("\n")

    def test_histogram_buckets_are_cumulative(self):
        m = Metrics()
        for d in (0.01, 0.2, 3.0):
            m.observe_request("/r", 200, d)
        lines = [l for l in m.render().splitlines() if "_bucket{" in l]
        values = [int(l.rsplit(" ", 1)[1]) for l in lines]
        assert values == sorted(values), "cumulative buckets must be non-decreasing"
        assert values[-1] == 3

    def test_label_escaping(self):
        m = Metrics()
        m.observe_request('/weird"route', 200, 0.1)
        assert '\\"' in m.render()


# ------------------------------------------------ route-level blame attribution


class _Stub:
    """Raises whatever it is told to, so error mapping can be exercised."""

    def __init__(self, exc=None):
        self.exc = exc
        from algorandscout.client import AlgorandConfig

        self.config = AlgorandConfig(network="mainnet")

    async def _boom(self, *a, **k):
        raise self.exc

    def __getattr__(self, name):
        return self._boom

    async def close(self):
        pass

    async def health(self):
        if self.exc:
            raise self.exc
        return {"healthy": True, "network": "mainnet"}


@pytest.fixture
def api(monkeypatch):
    """
    Yields (test_client, stub). Tests set `stub.exc` to choose the failure.

    The stub instance is created before the app starts and then mutated, because
    lifespan captures the client once — swapping a dict entry afterwards would
    leave the app holding the original object.
    """
    from algorandscout import service

    stub = _Stub()
    monkeypatch.setattr(service, "AlgorandClient", lambda *a, **k: stub)
    with TestClient(app) as c:
        yield c, stub


class TestBlameAttribution:
    def test_malformed_address_is_400_and_never_reaches_upstream(self, api):
        client, stub = api
        stub.exc = AlgorandError("should not be called", status=500)
        r = client.get("/api/v2/addresses/NOT-AN-ADDRESS")
        assert r.status_code == 400
        assert r.json()["kind"] == "validation"

    def test_checksum_failure_is_400(self, api):
        client, _stub = api
        typo = REAL_ADDRESS[:-1] + ("A" if REAL_ADDRESS[-1] != "A" else "B")
        assert client.get(f"/api/v2/addresses/{typo}").status_code == 400

    def test_negative_asset_id_is_400(self, api):
        client, _stub = api
        assert client.get("/api/v2/tokens/-5").status_code == 400

    def test_upstream_400_is_reported_as_400_not_502(self, api):
        """The upstream blamed the request. Calling that a 502 pages the wrong person."""
        client, stub = api
        stub.exc = AlgorandError("HTTP 400: bad param", status=400)
        assert client.get("/api/v2/tokens/1").status_code == 400

    def test_upstream_429_is_passed_through_as_429(self, api):
        client, stub = api
        stub.exc = AlgorandError("rate limited", status=429)
        assert client.get("/api/v2/tokens/1").status_code == 429

    def test_upstream_5xx_is_502(self, api):
        client, stub = api
        stub.exc = AlgorandError("indexer down", status=503)
        assert client.get("/api/v2/tokens/1").status_code == 502

    def test_not_found_is_404(self, api):
        client, stub = api
        stub.exc = NotFound("no such asset", status=404)
        assert client.get("/api/v2/tokens/1").status_code == 404


class TestProbesAndMeta:
    def test_liveness_does_not_depend_on_upstream(self, api):
        """An upstream outage must not get this process killed — restarting fixes nothing."""
        client, stub = api
        stub.exc = AlgorandError("indexer down", status=503)
        assert client.get("/livez").status_code == 200

    def test_readiness_does_depend_on_upstream(self, api):
        client, stub = api
        stub.exc = AlgorandError("indexer down", status=503)
        assert client.get("/readyz").status_code in (502, 503)

    def test_readiness_ok_when_healthy(self, api):
        client, _stub = api
        r = client.get("/readyz")
        assert r.status_code == 200 and r.json()["ready"] is True

    def test_metrics_endpoint_serves_exposition(self, api):
        client, _stub = api
        client.get("/livez")
        body = client.get("/metrics").text
        assert "algorandscout_requests_total" in body
        assert "algorandscout_uptime_seconds" in body

    def test_request_id_is_echoed(self, api):
        client, _stub = api
        r = client.get("/livez", headers={"X-Request-ID": "abc123"})
        assert r.headers["X-Request-ID"] == "abc123"

    def test_request_id_generated_when_absent(self, api):
        client, _stub = api
        assert client.get("/livez").headers.get("X-Request-ID")


class TestMetricCardinality:
    """
    Regression: `/api/v2/tokens/-5` was not collapsed, because "-5".isdigit() is
    False. A caller walking negative ids could mint one time series per request,
    which is how a metrics endpoint takes down the monitoring system.
    """

    def test_hostile_id_walking_collapses_to_one_label(self, api):
        client, _stub = api
        for asset_id in range(-1, -6, -1):
            client.get(f"/api/v2/tokens/{asset_id}")
        labels = [l for l in client.get("/metrics").text.splitlines() if "requests_total" in l]
        token_labels = {l.split("route=")[1].split(",")[0] for l in labels if "tokens" in l}
        assert len(token_labels) == 1, f"cardinality leak: {token_labels}"

    def test_unmatched_paths_share_one_bucket(self, api):
        client, _stub = api
        for i in range(5):
            client.get(f"/definitely/not/a/route/{i}")
        labels = [l for l in client.get("/metrics").text.splitlines() if "requests_total" in l]
        assert any('route="<unmatched>"' in l for l in labels)
        assert not any("/definitely/not" in l for l in labels)
