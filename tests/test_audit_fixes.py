# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Regression tests for the 2026-08-08 audit.

Each class below pins a defect that was real and shipped, not a hypothetical. Two of them
were the exact failure mode this project exists to prevent — an answer that looks correct
and is not — so they get named fixtures and named reasons.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from algorandscout.client import NETWORK_ENDPOINTS, AlgorandConfig
from algorandscout.mapping import map_asset_holders, map_transaction

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class TestNetworkUrlCoupling:
    """
    DEFECT: `ALGORAND_NETWORK=testnet` left the URLs at their mainnet defaults, so the
    service read mainnet and labelled every response `testnet`. Reporting one chain's state
    as another's is precisely the class of error this module claims to eliminate.
    """

    def test_testnet_selects_testnet_endpoints(self, monkeypatch):
        monkeypatch.setenv("ALGORAND_NETWORK", "testnet")
        monkeypatch.delenv("ALGORAND_ALGOD_URL", raising=False)
        monkeypatch.delenv("ALGORAND_INDEXER_URL", raising=False)
        config = AlgorandConfig()
        assert "testnet" in config.algod_url
        assert "testnet" in config.indexer_url
        assert "mainnet" not in config.algod_url

    @pytest.mark.parametrize("network", sorted(NETWORK_ENDPOINTS))
    def test_every_known_network_resolves_to_its_own_endpoints(self, network, monkeypatch):
        monkeypatch.setenv("ALGORAND_NETWORK", network)
        monkeypatch.delenv("ALGORAND_ALGOD_URL", raising=False)
        monkeypatch.delenv("ALGORAND_INDEXER_URL", raising=False)
        config = AlgorandConfig()
        expected_algod, expected_indexer = NETWORK_ENDPOINTS[network]
        assert config.algod_url == expected_algod
        assert config.indexer_url == expected_indexer

    def test_explicit_url_still_wins(self, monkeypatch):
        monkeypatch.setenv("ALGORAND_NETWORK", "testnet")
        config = AlgorandConfig(algod_url="https://my-own-node.example")
        assert config.algod_url == "https://my-own-node.example"

    def test_env_url_beats_network_default(self, monkeypatch):
        monkeypatch.setenv("ALGORAND_NETWORK", "testnet")
        monkeypatch.setenv("ALGORAND_ALGOD_URL", "https://private-node.example")
        assert AlgorandConfig().algod_url == "https://private-node.example"

    def test_unknown_network_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("ALGORAND_NETWORK", "mainnett")
        with pytest.raises(ValueError, match="unknown ALGORAND_NETWORK"):
            AlgorandConfig()

    def test_network_label_mismatch_is_warned_not_silent(self, monkeypatch, caplog):
        """A testnet label on a mainnet URL may be a proxy — but it never passes quietly."""
        monkeypatch.setenv("ALGORAND_NETWORK", "testnet")
        monkeypatch.setenv("ALGORAND_ALGOD_URL", "https://mainnet-api.algonode.cloud")
        with caplog.at_level("WARNING"):
            AlgorandConfig()
        assert any("config mismatch" in r.message for r in caplog.records)

    def test_network_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ALGORAND_NETWORK", "TestNet")
        assert AlgorandConfig().network == "testnet"


class TestCloseRemainder:
    """
    DEFECT: a `pay` carrying `close-remainder-to` sweeps the sender's entire remaining
    balance to a third address on top of `amount`. The mapper reported `amount` only, so the
    swept funds and the account closure were both invisible.

    Fixture `tx_pay_close.json` is a REAL mainnet transaction
    (OOQJ3LAGJ3YYEKIEKCAXZ4UMFI5FN3MOZCF3CYHIKA5NRXBYE6MA): 1_000_000 sent, 97_144 swept.
    """

    @pytest.fixture
    def mapped(self):
        return map_transaction(fixture("tx_pay_close.json")["transaction"])

    def test_value_still_means_the_explicit_amount(self, mapped):
        assert mapped["value"] == "1000000"

    def test_total_movement_includes_the_swept_remainder(self, mapped):
        assert mapped["value_total"] == "1097144"
        assert mapped["close"]["close_amount"] == "97144"

    def test_closure_is_flagged(self, mapped):
        assert mapped["closes_account"] is True
        assert mapped["close"]["close_remainder_to"] == "TIBGRAR2MGJHMIAWW7S26KIKUJA6NVIHDD6GYNODU67HPTKR5FALC7ASCA"

    def test_decimals_are_exact(self, mapped):
        assert mapped["close"]["value_total_decimal"] == "1.097144"

    def test_ordinary_payment_has_no_close_block(self):
        tx = {
            "id": "AAA",
            "tx-type": "pay",
            "sender": "S",
            "fee": 1000,
            "confirmed-round": 1,
            "payment-transaction": {"receiver": "R", "amount": 100},
        }
        mapped = map_transaction(tx)
        assert mapped["close"] is None
        assert mapped["closes_account"] is False
        assert mapped["value_total"] == "100"

    def test_zero_address_close_target_is_not_a_closure(self):
        """The all-zero address means 'unset', not 'closed to nobody'."""
        tx = {
            "id": "BBB",
            "tx-type": "pay",
            "sender": "S",
            "fee": 1000,
            "confirmed-round": 1,
            "payment-transaction": {
                "receiver": "R",
                "amount": 100,
                "close-remainder-to": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ",
            },
        }
        assert map_transaction(tx)["closes_account"] is False

    def test_axfer_close_ends_the_holding_not_the_account(self):
        tx = {
            "id": "CCC",
            "tx-type": "axfer",
            "sender": "S",
            "fee": 1000,
            "confirmed-round": 1,
            "asset-transfer-transaction": {
                "asset-id": 31566704,
                "receiver": "R",
                "amount": 10,
                "close-to": "CLOSER",
                "close-amount": 5,
            },
        }
        mapped = map_transaction(tx)
        assert mapped["close"]["closes_asset_holding"] is True
        assert mapped["closes_account"] is False, "closing an ASA opt-in is not closing the account"
        assert mapped["close"]["value_total"] == "15"
        assert mapped["close"]["asset_id"] == 31566704

    def test_falls_back_to_top_level_closing_amount(self):
        """Older indexer payloads put the swept amount only at the top level."""
        tx = {
            "id": "DDD",
            "tx-type": "pay",
            "sender": "S",
            "fee": 1000,
            "confirmed-round": 1,
            "closing-amount": 42,
            "payment-transaction": {"receiver": "R", "amount": 100, "close-remainder-to": "CLOSER"},
        }
        assert map_transaction(tx)["value_total"] == "142"


class TestAssetHoldersMapper:
    """DEFECT: holder mapping was inline in the route, therefore untested."""

    def test_maps_balances_with_decimals(self):
        payload = {
            "balances": [{"address": "A", "amount": 1_500_000, "is-frozen": False}],
            "next-token": "cursor",
        }
        mapped = map_asset_holders(payload, decimals=6)
        assert mapped["items"][0]["value"] == "1500000"
        assert mapped["items"][0]["value_decimal"] == "1.5"
        assert mapped["next_page_params"] == {"next_token": "cursor"}

    def test_without_decimals_no_decimal_rendering_is_invented(self):
        payload = {"balances": [{"address": "A", "amount": 1_500_000}]}
        assert map_asset_holders(payload)["items"][0]["value_decimal"] is None

    def test_empty_page_has_no_cursor(self):
        assert map_asset_holders({"balances": []})["next_page_params"] is None


class TestPassthroughAllowlist:
    """DEFECT: a bare startswith let `/v2/accountsX` through on the `/v2/accounts` prefix."""

    @staticmethod
    def _allowed(target: str) -> bool:
        from algorandscout.service import PASSTHROUGH_PREFIXES

        return any(target == p or target.startswith(p + "/") for p in PASSTHROUGH_PREFIXES)

    def test_exact_prefix_allowed(self):
        assert self._allowed("/v2/accounts")

    def test_child_path_allowed(self):
        assert self._allowed("/v2/accounts/ADDR/transactions")

    def test_lookalike_sibling_rejected(self):
        assert not self._allowed("/v2/accountsX")
        assert not self._allowed("/v2/accounts-internal/secrets")

    def test_unlisted_path_rejected(self):
        assert not self._allowed("/v2/status")
        assert not self._allowed("/v2/ledger/supply")
