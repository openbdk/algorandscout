# Algorandscout

**An explorer API for [Algorand](https://algorand.co/).** Accounts, assets, applications,
transactions and rounds — served as clean JSON over Algorand's own
[algod](https://developer.algorand.org/docs/rest-apis/algod/) and
[indexer](https://developer.algorand.org/docs/rest-apis/indexer/) APIs, with the parts of the
chain that have no equivalent elsewhere reported as themselves rather than flattened away.

Part of the [Open Blockchain Development Kit](https://github.com/openbdk).
Licensed under the **BANKON License** (Apache-2.0) — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

```bash
pip install -e '.[service]'
python -m algorandscout --port 8100
curl localhost:8100/api/v2/tokens/31566704
```

---

## What it serves

| Route | Returns |
|---|---|
| `GET /api/v2/capabilities` | **Start here.** What this API can and cannot answer, and why |
| `GET /api/v2/stats` | Chain tip, indexer lag, block time |
| `GET /api/v2/blocks` · `/blocks/{round}` | Rounds; `?include_transactions=` off by default |
| `GET /api/v2/transactions/{txid}` | One transaction, with inner transactions recursed |
| `GET /api/v2/addresses/{address}` | Account: balance, rekey target, counters; `?live=` reads the node |
| `GET /api/v2/addresses/{address}/transactions` | History; `after_time`/`before_time` (RFC-3339), `tx_type`, `asset_id` |
| `GET /api/v2/addresses/{address}/token-balances` | ASA holdings, metadata resolved by default |
| `GET /api/v2/tokens/{asset_id}` · `/holders` | ASA parameters, the four privileged roles, holder page |
| `GET /api/v2/smart-contracts/{app_id}` | Application: AVM programs, state schema, decoded global state |
| `GET /api/v2/search?q=` | Resolves by shape: address · txid · asset/app id · unit-name candidates |
| `GET /algorand/v2/*` | **Allowlisted** passthrough to the indexer — Algorand's own shapes, untranslated |

---

## The two rules that will bite you

**Holdings live on two surfaces.** `/addresses/{a}` carries the ALGO balance;
`/addresses/{a}/token-balances` carries the ASAs. Neither subsumes the other, and for most
accounts the native balance is the larger position. An answer built from one endpoint alone is
structurally incomplete.

**A transfer can move more than `value`.** A `pay` carrying `close-remainder-to` sweeps the
sender's entire remaining balance to a third address and closes the account; those funds are in
`close-amount`, never in `amount`. Read `value_total` and `closes_account`, not `value` alone.
The same applies to an `axfer` with `close-to`, which ends an ASA opt-in.

---

## What Algorand has that this API reports faithfully

These are the places a generic explorer would quietly lose information, so they get first-class
fields:

| Algorand concept | Why it matters |
|---|---|
| **ASA privileged roles** | An asset has manager, reserve, freeze and **clawback** addresses. A clawback holder can move an asset out of your account **without your signature** — by design, for regulated instruments. Flattened into a generic "token", that fact disappears. `clawback_enabled` and `roles` are always reported, and a clawback transfer is *named* as one. |
| **Close-remainder** | See above. Two numbers, always. |
| **Inner transactions** | Application calls emit their own transactions. Recursed, not summarised. |
| **Atomic groups** | `group` identifies transactions that succeeded or failed together. |
| **Rekeying** | An account's signing authority can be delegated to another address. `rekeyed_to` is reported; ignoring it misattributes who actually controls an account. |
| **Logic signatures** | `signature_type` distinguishes `ed25519`, `multisig` and `logicsig`. |
| **Boxes and state schemas** | Application storage is reported with its declared global/local schema. |
| **Finality** | Rounds are final on write. `is_final` is always true and there is no reorg depth to expose. |

## What Algorand does not have

Reported as `null`, never as a plausible substitute, each with a structural reason at
`/api/v2/capabilities`:

| Concept | Why it does not exist here |
|---|---|
| Gas price / gas used | Fees are flat; compute is a fixed opcode budget, not purchased. `fee` is reported in microAlgos and is a different quantity |
| Account nonce | Replay protection is a first-valid/last-valid round window plus the genesis hash. Reported as `validity` |
| Log topics | Application logs are ordered arrays of opaque bytes. There is nothing indexed to filter on |
| Verified contract source | There is no public verified-source registry for AVM programs; the approval and clear-state programs are available as compiled bytecode |
| Read-only VM invocation | Application *state* is readable directly (global, local, boxes), which answers most of the same questions without executing anything |
| Token allowances | ASAs have no approve/allowance model. Delegated spending is expressed with clawback and logic signatures, which are not equivalent |
| Reorgs and uncle blocks | One block per round with immediate finality. An empty uncle list means *structurally impossible*, not *none this round* |

**The address is not hex.** `hash` carries the 58-character base32 address verbatim. A client
validating `^0x[0-9a-fA-F]{40}$` will reject it — correctly. This API will not fabricate a
hex-shaped address to keep such a client quiet.

---

## Production surface

| Endpoint | Purpose |
|---|---|
| `GET /livez` | Liveness — **independent of the upstream**. An indexer outage must not get this process killed; restarting fixes nothing |
| `GET /readyz` | Readiness — **depends on the upstream**. Returns 503 so an instance that cannot answer leaves the load-balancer rotation |
| `GET /metrics` | Prometheus exposition: requests by route+status, latency histogram, upstream outcomes, cache hits |

**Blame attribution.** A malformed identifier is a **400** and never reaches the upstream —
Algorand addresses carry a checksum, so a typo is caught locally with certainty. An upstream 4xx
stays a 4xx, a 429 is passed through as 429, and only a genuine upstream failure is a **502**.
Returning 502 for a caller's typo pages the wrong person.

**Retry policy.** 5xx retried three times with jittered backoff; 4xx never — **except 429**,
which describes *when* a request arrived rather than what was in it. `Retry-After` is honoured
(delta-seconds only, clamped to 10s; the HTTP-date form is ignored rather than guessed at).
Jitter matters because a fleet that hits a limit together otherwise retries together.

**Caching.** Per-kind TTLs chosen from what the chain guarantees. Blocks and confirmed
transactions are final on Algorand and cached for an hour (~200× faster warm); assets and
applications for 5 minutes, since an `acfg` can reconfigure them. Accounts, balances and
transaction lists are **never** cached — they change every round, and a stale balance is exactly
the kind of wrong answer this project exists to avoid.

**Observability.** Every response carries `X-Request-ID` (echoed if supplied). Metric route
labels come from the routing table rather than the path, so a caller walking ids cannot mint
unbounded time series.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ALGORAND_NETWORK` | `mainnet` | `mainnet` · `testnet` · `betanet` · `localnet`. **The URL defaults derive from this** — setting the network alone is safe and cannot silently read another chain. An unknown value refuses to start |
| `ALGORAND_ALGOD_URL` | *derived* | the node — knows *now*. Explicit value always wins |
| `ALGORAND_INDEXER_URL` | *derived* | the archive — knows *history*. Explicit value always wins |
| `ALGORAND_API_TOKEN` / `_HEADER` | *(empty)* / `X-Algo-API-Token` | [AlgoNode](https://algonode.io/) is keyless; other providers are not |
| `ALGORAND_TIMEOUT_S` | `30` | per request |
| `OPENBDK_HOST` / `OPENBDK_PORT` | `127.0.0.1` / `8100` | service bind |

## Container

```bash
docker build -t algorandscout .
docker run -p 8100:8100 -e ALGORAND_NETWORK=mainnet algorandscout
```

Runs as a non-root user with a `HEALTHCHECK` on `/readyz`. CI runs the suite on Python
3.10–3.12, lints, builds the image and proves the container serves `/livez`.

---

## Read-only, structurally

There is no signing key, no write route, no transaction submission, and no path to add one by
accident — `tests/test_client.py::TestReadOnlyGuarantee` fails if a write-shaped method ever
appears on the client. An observer that can spend is a different and far more dangerous thing
than an observer.

## Tests

```bash
pip install -e '.[dev,service]'
pytest -q          # 186 tests, no network
```

Fixtures are **real responses captured from Algorand mainnet** — the USDC ASA (31566704), a
Tinyman application (1002541853), round 63879000, a live account, and a real closing payment —
not hand-written approximations. The assertions that matter most are the negative ones: that
`gas_used`, `nonce`, `abi` and a round's own hash come back `null` rather than invented.

## Compatibility

The REST layout (`/api/v2/addresses/…`, `/api/v2/tokens/…`, `{items, next_page_params}` pages)
follows conventions common to blockchain explorer APIs, so tooling written against that general
shape usually works against this one without modification. That is interface convention, not
lineage: Algorandscout is an independent work and contains no third-party explorer code. Where
Algorand's model and a generic explorer model disagree, **Algorand wins** and the difference is
declared at `/api/v2/capabilities` rather than papered over.

## Further reading

[Algorand developer docs](https://developer.algorand.org/) ·
[Indexer REST API](https://developer.algorand.org/docs/rest-apis/indexer/) ·
[algod REST API](https://developer.algorand.org/docs/rest-apis/algod/) ·
[ARC standards](https://github.com/algorandfoundation/ARCs) ·
[AlgoNode](https://algonode.io/) ·
[OpenBDK](https://github.com/openbdk)
