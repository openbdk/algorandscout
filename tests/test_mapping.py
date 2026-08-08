# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Mapping tests, run against fixtures captured from the LIVE Algorand mainnet indexer on
2026-08-08. No network, no mocks of our own invention — the inputs are real responses.

The assertions that matter most are the negative ones: that the mapper leaves a hole
where Algorand has no answer, instead of filling it with something plausible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from algorandscout import capabilities as caps
from algorandscout.mapping import (
    ALGO_DECIMALS,
    classify_asset,
    decimal_string,
    is_zero_address,
    map_account,
    map_account_assets,
    map_application,
    map_asset,
    map_block,
    map_stats,
    map_transaction,
    map_transaction_list,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ------------------------------------------------------------------ primitives


class TestDecimalString:
    def test_micro_algo_conversion(self):
        assert decimal_string(5_401_728_855, ALGO_DECIMALS) == "5401.728855"

    def test_trailing_zeros_stripped(self):
        assert decimal_string(1_500_000, ALGO_DECIMALS) == "1.5"
        assert decimal_string(1_000_000, ALGO_DECIMALS) == "1"

    def test_sub_unit_amount_keeps_leading_zero(self):
        assert decimal_string(1, ALGO_DECIMALS) == "0.000001"

    def test_zero(self):
        assert decimal_string(0, ALGO_DECIMALS) == "0"

    def test_zero_decimals_passthrough(self):
        assert decimal_string(42, 0) == "42"

    def test_none_stays_none(self):
        assert decimal_string(None, ALGO_DECIMALS) is None

    def test_negative(self):
        assert decimal_string(-1_500_000, ALGO_DECIMALS) == "-1.5"

    def test_uint64_max_is_exact(self):
        """The USDC ASA total is 2^64-1. A float would silently round it; this must not."""
        total = 18_446_744_073_709_551_615
        assert decimal_string(total, 6) == "18446744073709.551615"


class TestZeroAddress:
    def test_detects_algorand_zero_address(self):
        assert is_zero_address("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ")

    def test_real_address_is_not_zero(self):
        assert not is_zero_address("2UEQTE5QDNXPI7M3TU44G6SYKLFWLPQO7EBZM7K7MHMQQMFI4QJPLHQFHM")


# --------------------------------------------------------------------- account


class TestMapAccount:
    @pytest.fixture
    def mapped(self):
        return map_account(fixture("account.json"))

    def test_address_is_carried_verbatim(self, mapped):
        assert mapped["hash"] == "2UEQTE5QDNXPI7M3TU44G6SYKLFWLPQO7EBZM7K7MHMQQMFI4QJPLHQFHM"
        assert len(mapped["hash"]) == 58
        assert mapped["hash_format"] == "base32-ed25519"

    def test_address_is_not_hex_shaped(self, mapped):
        """A client validating for EVM hex must fail loudly rather than get a fake."""
        assert not mapped["hash"].startswith("0x")

    def test_balance_exact_and_decimal(self, mapped):
        assert mapped["coin_balance"] == "5401728855"
        assert mapped["coin_balance_decimal"] == "5401.728855"

    def test_rekey_surfaced(self, mapped):
        assert mapped["rekeyed_to"] == "5XTAQIQYGACAM234OQK34BIBSAFZFIBNBQWQCRVJHI65GUG6OUNBPYCJNQ"

    def test_multisig_signature_type(self, mapped):
        assert mapped["signature_type"] == "msig"

    def test_counters_from_real_account(self, mapped):
        assert mapped["counters"]["assets_created"] == 1
        assert mapped["counters"]["assets_held"] == 1
        assert mapped["counters"]["apps_created"] == 0

    def test_creator_of_asset_only_is_not_a_contract(self, mapped):
        """Creating an ASA does not make an account a contract. Only creating apps does."""
        assert mapped["is_contract"] is False

    def test_nonce_is_null_not_zero(self, mapped):
        """Algorand has no per-account nonce. Zero would be a lie; null is the truth."""
        assert mapped["nonce"] is None


# ----------------------------------------------------------------------- asset


class TestMapAsset:
    @pytest.fixture
    def mapped(self):
        return map_asset(fixture("asset_usdc.json"))

    def test_identity(self, mapped):
        assert mapped["asset_id"] == 31566704
        assert mapped["address"] == "31566704"
        assert mapped["name"] == "USDC"
        assert mapped["symbol"] == "USDC"
        assert mapped["decimals"] == 6

    def test_uint64_max_supply_survives(self, mapped):
        assert mapped["total_supply"] == "18446744073709551615"
        assert mapped["total_supply_decimal"] == "18446744073709.551615"

    def test_fungible_classification(self, mapped):
        assert mapped["type"] == "ASA"

    def test_privileged_roles_are_explicit(self, mapped):
        """Clawback is the whole reason ASA roles cannot be flattened into 'owner'."""
        assert mapped["roles"]["creator"] == "2UEQTE5QDNXPI7M3TU44G6SYKLFWLPQO7EBZM7K7MHMQQMFI4QJPLHQFHM"
        assert mapped["freeze_enabled"] is True
        assert mapped["roles"]["freeze"] == "3ERES6JFBIJ7ZPNVQJNH2LETCBQWUPGTO4ROA6VFUR25WFSYKGX3WBO5GE"

    def test_zero_clawback_address_reads_as_disabled(self, mapped):
        """USDC's clawback is the all-zero address, i.e. unset — not an account."""
        assert mapped["roles"]["clawback"] is None
        assert mapped["clawback_enabled"] is False

    def test_holders_count_not_fabricated(self, mapped):
        assert mapped["holders_count"] is None


class TestClassifyAsset:
    def test_nft_convention(self):
        assert classify_asset({"total": 1, "decimals": 0}) == "ASA-NFT"

    def test_edition_convention(self):
        assert classify_asset({"total": 100, "decimals": 0}) == "ASA-NFT-EDITION"

    def test_large_zero_decimal_supply_is_fungible(self):
        assert classify_asset({"total": 1_000_000_000, "decimals": 0}) == "ASA"

    def test_decimal_bearing_asset_is_fungible(self):
        assert classify_asset({"total": 1, "decimals": 6}) == "ASA"


# ----------------------------------------------------------------- application


class TestMapApplication:
    @pytest.fixture
    def mapped(self):
        return map_application(fixture("application.json"))

    def test_identity_and_language(self, mapped):
        assert mapped["app_id"] == 1002541853
        assert mapped["language"] == "TEAL/AVM"
        assert mapped["is_contract"] is True

    def test_programs_present_as_bytecode(self, mapped):
        assert mapped["approval_program"]
        assert isinstance(mapped["approval_program"], str)

    def test_no_solidity_abi_claimed(self, mapped):
        assert mapped["abi"] is None
        assert mapped["source_code"] is None
        assert mapped["is_verified"] is False
        assert mapped["compiler_version"] is None

    def test_global_state_decoded_with_raw_preserved(self, mapped):
        entries = mapped["global_state"]
        assert entries, "fixture app should carry global state"
        for entry in entries:
            assert "key_b64" in entry
            assert entry["type"] in {"bytes", "uint"}

    def test_readable_keys_are_decoded(self, mapped):
        keys = {e["key"] for e in mapped["global_state"] if e["key"]}
        assert keys, "at least one global-state key should decode to text"


# ----------------------------------------------------------------------- block


class TestMapBlock:
    @pytest.fixture
    def mapped(self):
        return map_block(fixture("block.json"))

    def test_round_is_height(self, mapped):
        assert mapped["height"] == 63879000

    def test_own_hash_is_null_not_the_parent(self, mapped):
        """The indexer returns no own-hash for a round. Reusing the parent's would be wrong."""
        assert mapped["hash"] is None
        assert mapped["parent_hash"] == "ffzOqujebJOIcFmyXCIPU8uL5NscVZ+88PK+FQLEOt4="

    def test_proposer_maps_to_miner(self, mapped):
        assert mapped["proposer"] == "6SBJ63VWR5BT67WCEPTGCRQQ32MB4DQAGHRLYRORAGIAC54FK6F2IG2JSY"
        assert mapped["miner"]["hash"] == mapped["proposer"]

    def test_timestamp_iso_and_unix(self, mapped):
        assert mapped["timestamp_unix"] == 1786211006
        assert mapped["timestamp"].endswith("Z")

    def test_finality_is_asserted(self, mapped):
        assert mapped["is_final"] is True
        assert mapped["uncles_hashes"] == []

    def test_gas_fields_are_null(self, mapped):
        for field in ("gas_used", "gas_limit", "base_fee_per_gas", "difficulty"):
            assert mapped[field] is None, f"{field} must be null on a non-EVM chain"

    def test_fees_collected_decimal(self, mapped):
        assert mapped["fees_collected"] == 28000
        assert mapped["fees_collected_decimal"] == "0.028"

    def test_transactions_omitted_by_default(self, mapped):
        assert "transactions" not in mapped

    def test_transactions_included_on_request(self):
        mapped = map_block(fixture("block.json"), include_transactions=True)
        assert isinstance(mapped["transactions"], list)
        assert len(mapped["transactions"]) == mapped["transactions_count"]


# ----------------------------------------------------------------- transaction


class TestMapTransaction:
    @pytest.fixture
    def tx(self):
        return fixture("transactions.json")["transactions"][0]

    def test_identity_and_status(self, tx):
        mapped = map_transaction(tx)
        assert mapped["hash"] == tx["id"]
        assert mapped["status"] == "ok"
        assert mapped["block_number"] == tx["confirmed-round"]

    def test_fee_in_micro_algos(self, tx):
        mapped = map_transaction(tx)
        assert mapped["fee"] == "1000"
        assert mapped["fee_decimal"] == "0.001"

    def test_gas_fields_null(self, tx):
        mapped = map_transaction(tx)
        for field in ("gas_used", "gas_price", "gas_limit", "nonce", "revert_reason"):
            assert mapped[field] is None

    def test_logicsig_signature_detected(self, tx):
        mapped = map_transaction(tx)
        assert mapped["signature_type"] == "logicsig"

    def test_acfg_creation_surfaces_created_asset(self, tx):
        mapped = map_transaction(tx)
        assert mapped["tx_type"] == "acfg"
        assert mapped["method"] == "Asset Configuration"
        assert mapped["created_asset_index"] == 31566704

    def test_validity_window_replaces_nonce(self, tx):
        mapped = map_transaction(tx)
        assert mapped["validity"]["first"] == tx["first-valid"]
        assert mapped["validity"]["last"] == tx["last-valid"]

    def test_payment_movement(self):
        tx = {
            "id": "AAA",
            "tx-type": "pay",
            "sender": "SENDER",
            "fee": 1000,
            "confirmed-round": 100,
            "round-time": 1_700_000_000,
            "payment-transaction": {"receiver": "RECEIVER", "amount": 2_500_000},
        }
        mapped = map_transaction(tx)
        assert mapped["to"]["hash"] == "RECEIVER"
        assert mapped["value"] == "2500000"
        assert mapped["value_decimal"] == "2.5"

    def test_asset_transfer_produces_token_transfer(self):
        tx = {
            "id": "BBB",
            "tx-type": "axfer",
            "sender": "SENDER",
            "fee": 1000,
            "confirmed-round": 100,
            "asset-transfer-transaction": {"asset-id": 31566704, "receiver": "RECEIVER", "amount": 1_000_000},
        }
        mapped = map_transaction(tx)
        transfer = mapped["token_transfer"]
        assert transfer["asset_id"] == 31566704
        assert transfer["amount"] == "1000000"
        assert transfer["is_clawback"] is False

    def test_clawback_is_named_as_clawback(self):
        """
        An axfer with a `sender` inside the detail block moved someone else's funds
        without their signature. Presenting that as an ordinary transfer would
        misrepresent consent, so it is flagged.
        """
        tx = {
            "id": "CCC",
            "tx-type": "axfer",
            "sender": "CLAWBACK_AUTHORITY",
            "fee": 1000,
            "confirmed-round": 100,
            "asset-transfer-transaction": {
                "asset-id": 31566704,
                "receiver": "RECEIVER",
                "sender": "VICTIM",
                "amount": 500,
            },
        }
        assert map_transaction(tx)["token_transfer"]["is_clawback"] is True

    def test_inner_transactions_recurse(self):
        tx = {
            "id": "DDD",
            "tx-type": "appl",
            "sender": "SENDER",
            "fee": 2000,
            "confirmed-round": 100,
            "application-transaction": {"application-id": 1002541853},
            "inner-txns": [
                {
                    "id": "DDD-inner",
                    "tx-type": "pay",
                    "sender": "APP",
                    "fee": 0,
                    "confirmed-round": 100,
                    "payment-transaction": {"receiver": "USER", "amount": 1},
                }
            ],
        }
        mapped = map_transaction(tx)
        assert mapped["internal_transactions_count"] == 1
        assert mapped["internal_transactions"][0]["to"]["hash"] == "USER"

    def test_app_call_targets_app_id(self):
        tx = {
            "id": "EEE",
            "tx-type": "appl",
            "sender": "SENDER",
            "fee": 1000,
            "confirmed-round": 100,
            "application-transaction": {"application-id": 1002541853},
        }
        assert map_transaction(tx)["to"]["hash"] == "1002541853"

    def test_unknown_type_degrades_without_crashing(self):
        mapped = map_transaction({"id": "FFF", "tx-type": "future-type", "sender": "S", "fee": 1000})
        assert mapped["to"] is None
        assert mapped["status"] == "pending"
        assert mapped["method"] == "future-type"


class TestMapTransactionList:
    def test_confirmations_from_chain_tip(self):
        payload = fixture("transactions.json")
        mapped = map_transaction_list(payload, chain_tip=63_879_061)
        first = mapped["items"][0]
        assert first["confirmations"] == 63_879_061 - first["block_number"]

    def test_next_page_params_carry_cursor(self):
        payload = fixture("transactions.json")
        mapped = map_transaction_list(payload)
        assert mapped["next_page_params"]["next_token"] == payload["next-token"]

    def test_no_cursor_means_no_next_page(self):
        assert map_transaction_list({"transactions": []})["next_page_params"] is None


# ----------------------------------------------------------- balances & stats


class TestMapAccountAssets:
    def test_holding_without_resolved_metadata(self):
        payload = {"assets": [{"asset-id": 31566704, "amount": 1_500_000, "is-frozen": False}]}
        item = map_account_assets(payload)["items"][0]
        assert item["value"] == "1500000"
        assert item["value_decimal"] is None  # decimals unknown without the ASA params

    def test_holding_with_resolved_metadata(self):
        payload = {"assets": [{"asset-id": 31566704, "amount": 1_500_000, "is-frozen": False}]}
        params = {31566704: {"name": "USDC", "unit-name": "USDC", "decimals": 6, "total": 1}}
        item = map_account_assets(payload, params)["items"][0]
        assert item["value_decimal"] == "1.5"
        assert item["token"]["symbol"] == "USDC"


class TestMapStats:
    def test_lag_and_non_evm_flag(self):
        status = {"last-round": 63_879_061, "time-since-last-round": 2_800_000_000}
        health = {"indexer_round": 63_879_058, "indexer_lag_rounds": 3, "network": "mainnet"}
        mapped = map_stats(status, health)
        assert mapped["chain_tip"] == 63_879_061
        assert mapped["indexer_lag_rounds"] == 3
        assert mapped["is_evm"] is False
        assert mapped["gas_prices"] is None


# ---------------------------------------------------------------- capabilities


class TestCapabilities:
    def test_declares_non_evm(self):
        assert caps.CHAIN["is_evm"] is False
        assert caps.CHAIN["reorgs"] is False

    def test_every_unsupported_entry_has_a_reason(self):
        for feature, reason in caps.UNSUPPORTED.items():
            assert len(reason) > 40, f"{feature} needs a real structural reason, not a stub"

    def test_supported_and_unsupported_do_not_overlap(self):
        assert not set(caps.SUPPORTED) & set(caps.UNSUPPORTED)

    def test_payload_is_serialisable(self):
        json.dumps(caps.capabilities())

    def test_the_known_traps_are_documented(self):
        # Key names are chain-native: Algorand has ASAs and no allowance model, so the
        # entry is `token_allowance`, not an EVM-flavoured `erc20_allowance`.
        for trap in ("logs_by_topic", "gas_price", "token_allowance", "nonce", "contract_abi"):
            assert caps.why_unsupported(trap)
