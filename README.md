# Algorandscout

**A Blockscout-shaped read API for Algorand.** Point a Blockscout-compatible client at it
and read Algorand accounts, assets, applications, transactions and rounds — without
pretending Algorand is an EVM chain.

Part of the [Open Blockchain Development Kit](https://github.com/openbdk).
Licensed under the **BANKON License** (Apache-2.0) — see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE).

```bash
pip install -e '.[service]'
python -m algorandscout --port 8100
curl localhost:8100/api/v2/tokens/31566704
```

---

## Read this first: why this is a separate service, not a Blockscout fork

The obvious build — add an `algorand` chain type to
[blockscout/blockscout](https://github.com/blockscout/blockscout) — is the wrong build,
for two independent reasons. Either one alone would be decisive.

### 1. Blockscout's licence forbids it

Blockscout re-licensed on **2026-04-22**. It is no longer open source. The
[Blockscout Software Licence](https://github.com/blockscout/blockscout/blob/master/LICENSE)
(SPDX `LicenseRef-Blockscout`) says, in clause 5:

> **(a)** you may create Derivative Works of the Software **solely for your internal use**.
> **(b)** You shall **not distribute, sublicense, sell, license, make available, or otherwise
> provide any Derivative Works**, in whole or in part, to any third party without first
> obtaining a Commercial Licence.
> **(c)** you hereby grant the Licensor a **perpetual, irrevocable, worldwide, royalty-free,
> non-exclusive, transferable, and sublicensable licence** to use, reproduce, modify, adapt,
> incorporate, and otherwise exploit any Derivative Works for any purpose.

An Algorand module written *into* a Blockscout tree is a Derivative Work. Publishing that
tree to a public repository is "making available to a third party". So a merged-in module
could not be published at all without a Commercial Licence — and the moment it existed,
clause 5(c) would hand Blockscout Limited an irrevocable, sublicensable licence over it.
Clause 4(a) separately bars monetised or hosted use, which rules out running it as a
service. Clause 7(c) asserts that the Software "as a whole, and all parts thereof" is under
their licence, so a BANKON licence on a merged-in module would be contradicted by the tree
it sits in.

The same licence supplies the exit, in its own definition of a Derivative Work:

> *"For the avoidance of doubt, Derivative Works do not include works that remain
> **separable from**, or **merely link to**, the Software."*

This module is exactly that: a separate process, a separate repository, no Blockscout
source, no Blockscout dependency. It speaks a compatible response **shape** over its own
HTTP surface. Interface compatibility is not derivation. That is what makes the BANKON
licence on this code real rather than decorative.

**The constraint this imposes is permanent:** do not merge this module into a Blockscout
source tree, do not vendor Blockscout code into it, do not ship it inside a Blockscout
release artifact. Deploy the two side by side. [`NOTICE`](NOTICE) states this in the terms
the licence uses.

*(This is engineering rationale, not legal advice. Anyone shipping Blockscout itself
commercially should read the licence and take their own counsel.)*

### 2. Algorand does not fit Blockscout's schema

Blockscout's core is EVM to the bone. Its supported chain types — verified in
`config/config_helper.exs` on the current master — are `arbitrum`, `arc`, `blackfort`,
`eden`, `ethereum`, `filecoin`, `optimism`, `rsk`, `scroll`, `shibarium`, `stability`,
`suave`, `zetachain`, `zilliqa`, `zksync`, `neon`, `optimism-celo`. Every single one is
EVM. That is not an oversight; the schema assumes 20-byte addresses, gas, nonces, logs
with indexed topics, and reorgs.

Algorand has none of those. Addresses are 58-character base32 Ed25519. Fees are flat and
compute is a fixed opcode budget, not a market. Replay protection is a validity-round
window, not a nonce. Application logs are opaque byte arrays with no indexed topics.
Blocks are final on write. Forcing that into the EVM tables would produce a schema full of
plausible-looking nulls and one genuinely dangerous lie — an ASA presented as an ERC-20,
hiding the fact that **clawback can move a holder's balance without the holder signing**.

So this module maps honestly and says where the map ends.

---

## What it serves

### Blockscout-shaped (`/api/v2/*`)

| Route | Returns |
|---|---|
| `GET /api/v2/capabilities` | **Start here.** What this module can and cannot answer, and why |
| `GET /api/v2/stats` | Chain tip, indexer lag, block time, `is_evm: false` |
| `GET /api/v2/blocks` · `/blocks/{round}` | Rounds. `?include_transactions=` off by default |
| `GET /api/v2/transactions/{txid}` | One transaction, with inner transactions recursed |
| `GET /api/v2/addresses/{address}` | Account: native balance, rekey, counters, `?live=` for node state |
| `GET /api/v2/addresses/{address}/transactions` | History. `after_time`/`before_time` (RFC-3339), `tx_type`, `asset_id` |
| `GET /api/v2/addresses/{address}/token-balances` | ASA holdings, metadata resolved by default |
| `GET /api/v2/tokens/{asset_id}` · `/holders` | ASA params, the four privileged roles, holder page |
| `GET /api/v2/smart-contracts/{app_id}` | Application: AVM programs, state schema, decoded global state |
| `GET /api/v2/search?q=` | Resolves by shape: address, txid, asset/app id, or unit-name candidates |

### Native (`/algorand/v2/*`)

Allowlisted passthrough to the indexer — Algorand's own shapes, untranslated. Allowlist,
not prefix-match, so it cannot become an open proxy to whatever the upstream adds later.

### `GET /health`

Both upstreams, and how far the archive trails the node.

---

## The two rules that will bite you

**Holdings live on two surfaces.** `/addresses/{a}` carries the ALGO balance;
`/addresses/{a}/token-balances` carries the ASAs. Neither subsumes the other, and for most
accounts the native balance is the larger position. An answer built from one endpoint is
structurally incomplete. (This is the same fork Blockscout's own analysis guidance warns
about on EVM chains.)

**The address is not hex.** `hash` carries the 58-character base32 address verbatim. A
client validating `^0x[0-9a-fA-F]{40}$` will reject it — and that rejection is *correct*.
This module will not fabricate a hex-shaped address to keep such a client quiet.

---

## Honest mapping table

| Blockscout concept | Algorand | Fidelity |
|---|---|---|
| block | round | **exact** — plus finality; no uncles, no reorgs |
| transaction | transaction (7 types) | **exact** |
| internal transaction | inner transaction | **exact** |
| address | account | **format differs** — base32, 58 chars |
| native coin | ALGO (microAlgos, 6dp) | **exact** |
| ERC-20 | ASA | **lossy** — no allowance; adds freeze/clawback/manager/reserve |
| ERC-721/1155 | ASA, total=1 decimals=0 | **heuristic** — a convention (ARC-3/19/69), not an on-chain type |
| smart contract | application | **lossy** — TEAL/AVM bytecode, no Solidity ABI, no verified source |
| logs + topics | app-call logs | **partial** — logs yes, **topics do not exist**; no `topic0` filtering |
| gas used / price | flat fee + opcode budget | **absent** — `fee` in microAlgos is reported, and is not the same quantity |
| nonce | first-valid/last-valid window | **absent** — reported as `validity`, never as a nonce |
| `eth_call` | — | **absent** — read app state (global/local/boxes) instead |

Every "absent" above is `null` in the response and carries a structural reason in
`/api/v2/capabilities`. Nothing is filled with a plausible substitute.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ALGORAND_ALGOD_URL` | `https://mainnet-api.algonode.cloud` | the node — knows *now* |
| `ALGORAND_INDEXER_URL` | `https://mainnet-idx.algonode.cloud` | the archive — knows *history* |
| `ALGORAND_API_TOKEN` | *(empty)* | AlgoNode is keyless; other providers are not |
| `ALGORAND_API_TOKEN_HEADER` | `X-Algo-API-Token` | header name differs per provider |
| `ALGORAND_NETWORK` | `mainnet` | `mainnet` · `testnet` · `betanet` · `localnet` |
| `ALGORAND_TIMEOUT_S` | `30` | per request |
| `OPENBDK_HOST` / `OPENBDK_PORT` | `127.0.0.1` / `8100` | service bind |

Retry policy matches the rule Blockscout publishes for its own upstreams: **5xx retried
three times, 4xx never** — 4xx responses are deterministic and retrying only wastes the call.

---

## Read-only, structurally

There is no signing key, no write route, no transaction submission, and no path to add one
by accident — `tests/test_client.py::TestReadOnlyGuarantee` fails if a write-shaped method
ever appears on the client. An observer that can spend is a different and far more
dangerous thing than an observer.

---

## Tests

```bash
pip install -e '.[dev,service]'
pytest -q          # 86 tests, no network
```

Fixtures in `tests/fixtures/` are **real responses captured from Algorand mainnet on
2026-08-08** — the USDC ASA (31566704), a Tinyman application (1002541853), round
63879000, and a live account — not hand-written approximations. The assertions that matter
most are the negative ones: that `gas_used`, `nonce`, `abi` and a round's own `hash` come
back `null` rather than as something invented.

---

## Deploying next to Blockscout

Two services, two ports, one reverse proxy. Route `/algorand/*` to this module and
everything else to Blockscout; neither process imports the other, and the separation that
makes the licensing work is preserved by the topology itself.

If you run Blockscout, its licence obligations are yours independently — including the
interface attribution requirement (clause 2(c)) and the restriction on commercial or hosted
use without a Commercial Licence (clause 4(a)). This module imposes none of that on you and
removes none of it either.

---

## Further reading

[Algorand developer docs](https://developer.algorand.org/) ·
[Indexer REST API](https://developer.algorand.org/docs/rest-apis/indexer/) ·
[algod REST API](https://developer.algorand.org/docs/rest-apis/algod/) ·
[ARC standards](https://github.com/algorandfoundation/ARCs) ·
[AlgoNode](https://algonode.io/) ·
[Blockscout](https://github.com/blockscout/blockscout) ·
[Blockscout Software Licence](https://github.com/blockscout/blockscout/blob/master/LICENSE) ·
[OpenBDK](https://github.com/openbdk)
