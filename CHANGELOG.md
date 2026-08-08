# Changelog

All notable changes to Algorandscout are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-08-08

First stable release. The `/api/v2` response shapes and the configuration
surface are now covered by semantic versioning: they will not change
incompatibly without a major bump.

### What 1.0.0 does *not* claim

Stated plainly, because a version number is a promise and an inflated one is a
lie. This release is **built, tested and published — not yet operated.** It has
never run under production traffic, has not been load-tested, and nothing
consumes it in production yet. What is stable is the interface and the
behaviour under test, not a track record.

### Added

- **Explorer API** over Algorand's algod and indexer: accounts, ASA holdings and
  metadata, applications with decoded global state, transactions with inner
  transactions recursed, rounds, and shape-aware search.
- **Allowlisted passthrough** at `/algorand/v2/*` for Algorand's own untranslated
  shapes. An allowlist, not a prefix match — the passthrough must not become an
  open proxy to whatever the upstream adds later.
- **`/api/v2/capabilities`** — a machine-readable statement of what the chain
  cannot answer and why, so a client learns the boundary before building a query
  on a field that will never be populated.
- **Production surface** — `/livez` (independent of the upstream: an indexer
  outage must not get the process killed), `/readyz` (dependent on it: leave the
  load-balancer rotation instead), `/metrics` in Prometheus exposition format
  with no Prometheus client dependency, and `X-Request-ID` correlation.
- **Per-kind caching** keyed to what the chain guarantees. Blocks and confirmed
  transactions are final on Algorand and cached for an hour (~200× faster warm);
  assets and applications for 5 minutes, since an `acfg` can reconfigure them.
  Accounts, balances and transaction lists are never cached.
- **Local input validation.** Algorand addresses carry a checksum, so a typo is
  caught with certainty before any network round trip or metered credit is spent.
- **Container** (non-root, `HEALTHCHECK` on `/readyz`) and **CI** across Python
  3.10–3.12 with lint, format check, image build and a container smoke test. The
  suite is offline by design: a third party's outage must not redden a good build.

### Fidelity — what this API refuses to flatten

- **ASA privileged roles.** An asset has manager, reserve, freeze and
  **clawback** addresses. A clawback holder can move an asset out of an account
  **without the holder signing**. Rendered as a generic token that fact
  disappears, so the roles are always reported and a clawback transfer is *named*
  as one.
- **Close-remainder.** A payment carrying `close-remainder-to` sweeps the
  sender's entire remaining balance and closes the account; that amount never
  appears in `amount`. Both `value` and `value_total` are reported.
- **Inner transactions, atomic groups, rekeying, logic signatures, boxes,
  declared state schemas, finality** — all first-class rather than summarised.
- **Absent concepts return `null`, never a substitute.** Gas, nonces, log topics,
  verified contract source, read-only VM invocation, token allowances, reorgs and
  uncle blocks do not exist on Algorand. Each carries a structural reason in
  `capabilities.UNSUPPORTED`.
- **Integer-exact amounts.** The USDC ASA total is 2⁶⁴−1; nothing here touches a
  float.

### Fixed — defects found and closed before this release

Every one was found by probing the running service rather than re-reading code.

- **Network label decoupled from endpoints.** `ALGORAND_NETWORK=testnet` with
  default URLs read mainnet and labelled the answers testnet. Endpoint defaults
  now derive from the network; an unknown network refuses to start rather than
  guess which chain to read.
- **Close-remainder was dropped**, understating a real mainnet transaction by the
  swept amount and hiding the account closure entirely.
- **429 was treated as permanent.** Rate limiting describes *when* a request
  arrived, not what was in it. Now retried with jittered backoff and a clamped
  `Retry-After`.
- **Caller errors were reported as 502**, telling operators the indexer was
  broken when a caller had mistyped an address.
- **Metric label cardinality leak.** `/api/v2/tokens/-5` was not collapsed
  (`"-5".isdigit()` is False), letting a caller mint unbounded time series.
  Labels now come from the routing table, not a path heuristic.
- **Serial N+1 metadata resolution** on holdings pages, replaced with a bounded
  fan-out.
- **Passthrough prefix matching** admitted `/v2/accountsX` on the `/v2/accounts`
  entry; now segment-aware.

### Security

- **Read-only by construction.** No signing key, no write route, no transaction
  submission, and a test that fails if a write-shaped method appears on the
  client. An observer that can spend is a different and far more dangerous thing.

### Removed

- `metrics.route_label`, superseded by routing-table lookup. Keeping the weaker
  implementation of the same idea beside the correct one only invites its reuse.

### Notes

Algorandscout is an original work under the BANKON License (Apache-2.0), part of
the [Open Blockchain Development Kit](https://github.com/openbdk). It contains no
third-party explorer source code. Its REST layout follows conventions common to
explorer APIs so existing tooling interoperates — a compatibility property, not a
lineage. Where Algorand's model and a generic explorer model disagree, Algorand
governs and the difference is declared rather than papered over.

185 tests, offline, against fixtures captured from live Algorand mainnet.
