# Telegraph Token-Risk Preflight Guard (`telegraph-token-guard`)

> **Telegraph Protocol Season II — Track 1 (Apps & Agents) · Mission 14: Web3 Native & DeFi (`telegraphprotocol/bounties#202`)**

An autonomous DeFi pre-trade safety gate that purchases bounded on-chain risk intelligence via **Telegraph Protocol (`https://backend.telegraphprotocol.com`)** before executing a simulated trade, emitting a deterministic **`ALLOW` / `REVIEW` / `REJECT`** verdict backed by cryptographically hashed JSONL payment receipts.

## Commercial Summary (Customer-First)

- **Target Customer:** Autonomous DeFi trading bots, treasury rebalancing agents, and memecoin/DEX execution routers on Ethereum, Base, Arbitrum, and Solana.
- **Problem Today:** Trading agents routinely swap into honeypots, high-sell-tax tokens, unlocked-LP rugs, or wash-traded pools because integrating 3–5 separate security/analytics vendors requires multiple API keys, subscriptions, and brittle scrapers.
- **Willingness to Pay & Unit Economics:** Paying **~$0.03 per preflight** (3 bounded Telegraph queries at ~$0.01 each) on a $1,000–$10,000 trade equals **0.3–3.0 bps** — orders of magnitude cheaper than a single rug pull or failed honeypot exit.
- **Network Disclosure:** Live verification runs on **Sepolia Testnet** via Anjoman Escrow API keys (`Authorization: Bearer anjoman_...`). Offline `--dry-run` outputs are explicitly tagged `mode: "dry_run_fixture"` and are never presented as live network calls.

## Architecture

1. **Zero-Spend Input Validation:** Rejects unsupported chains, malformed EVM/Solana addresses, or non-positive notional sizes at **$0.00 cost** before touching Telegraph.
2. **Hard Spending Cap (`--max-spend-usd`):** Enforces a strict per-session escrow budget so runaway loops cannot drain an agent's account.
3. **3-Pillar Telegraph Intelligence Purchase:**
   - `SMART_CONTRACT_AUDIT` — honeypot, sell tax, mint/freeze authority, proxy upgradability.
   - `WASH_TRADING_DETECTION` — circular wash volume ratio, top-10 holder concentration, LP lock.
   - `ONCHAIN_METRIC_VERIFY` — pool liquidity depth vs trade notional, contract age, active holders.
4. **Async 202 Polling & Fail-Closed Policy:** Automatically polls `GET /agent/jobs/{jobId}` on `202 Accepted` responses. Any `partial`, `failed`, `unsupported`, budget-blocked, or HTTP error penalizes risk score and fails closed (`REVIEW`/`REJECT`).
5. **Verifiable Receipt Ledger:** Appends every call's `jobId`, `conversationId`, `messageId`, `status`, `costUsd`, `latencyMs`, `sourcesCount`, and `answer_sha256` to `receipts/preflight_receipts.jsonl`.

## Quick Start (Zero External Dependencies — Python 3.10+)

### 1. Run the Full Unit & Failure-Case Test Suite (Offline)
```bash
python -m unittest discover -s tests -v
```

### 2. Try Offline Dry-Run Scenarios (`safe`, `review`, `honeypot`)
```bash
python cli.py --dry-run --dry-scenario safe --chain ethereum --address 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 --symbol USDC --notional-usd 2500
python cli.py --dry-run --dry-scenario honeypot --chain base --address 0x1111111111111111111111111111111111111111 --symbol SCAMX --notional-usd 1000
```

### 3. Run Live on Telegraph Protocol (Sepolia Testnet)
1. Sign in at [anjoman.ai](https://anjoman.ai), claim testnet USDC at `/dashboard/faucet`, and deposit into Escrow.
2. Create an API key at `/dashboard/api-keys` (starts with `anjoman_`).
3. Set the key and run a live preflight:
```bash
export TELEGRAPH_API_KEY="anjoman_your_key_here"
python cli.py --chain ethereum --address 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 --symbol USDC --notional-usd 2500 --max-spend-usd 0.15 --out reports/live_usdc_preflight.json
```

## Verified Live Sepolia Testnet Evidence (November 24, 2026)

All live runs below were executed on **Sepolia Testnet** via Anjoman Escrow (`0x3E4252144d24b622692CE82278E55623146AA8f2`, faucet tx `0x53d49ed76774afd8624a0b332b9b8accff0f1365076a898c5209e9996c43a1ad`) against `https://backend.telegraphprotocol.com/agent/chat` over SSE streaming (`text/event-stream`). Full JSON reports live in [`reports/`](reports/) and SHA-256 hashed receipts in [`receipts/live_sepolia_receipts.jsonl`](receipts/live_sepolia_receipts.jsonl).

| Run | Mode / Network | Telegraph `jobId` | Specialist Miners & `receiptHash` | Cost (USD) | Verdict |
|---|---|---|---|---|---|
| Ethereum `USDC` Unified Preflight | `live` (`sepolia-testnet`) | `a7985e76-b240-4f4f-8869-069305eedb68` | `vulnrank-triage` (`CRYPTO_PRICE_USD`, `0x226477...2ed55`, `$1.0000`); `sentinel-risk-oracle` (`LIQUIDITY_DEPTH_VERIFY_GENERAL`, `0x455cf7...a3e8e`, `$3.89M` depth, `<0.05%` slippage) | `$0.06` (`0.24 bps` on `$2,500`) | `REVIEW` (fail-closed: `partial`) |
| Ethereum `USDC` Canonical Preflight | `live` (`sepolia-testnet`) | `ec20b24a-566e-4276-91f9-25170df69667` | `vulnrank-triage` (`CRYPTO_PRICE_USD`, `0x0013ed...cb50d` & `0x40727f...5d787`, `$0.9999–$0.99995`) | `$0.04` (`0.16 bps` on `$2,500`) | `REVIEW` (fail-closed: `partial`) |
| Ethereum `USDC` Strict Audit (Failure Case) | `live` (`sepolia-testnet`) | `730bff3c-021f-460e-98d5-09293ee94b53` | 5 miner attempts -> `provider_unavailable` | `$0.03` | `REJECT` (fail-closed) |
| Malformed Address (Failure Case) | `live` (`sepolia-testnet`) | *(blocked locally)* | Blocked before network call | `$0.00` | `REJECT` |

### Honest Lessons Learned from Live Testing
1. **Use SSE streaming (`text/event-stream`) for `/agent/chat`:** Synchronous `"stream": false` requests hit Cloudflare `HTTP 504` when multi-miner orchestration exceeds ~60 seconds. Switching `telegraph_client.py` to SSE streaming with `: keep-alive` handling eliminated 504s completely.
2. **Fail-closed is essential on `partial` / `provider_unavailable`:** On Sepolia Testnet, `CRYPTO_PRICE_USD` and `LIQUIDITY_DEPTH_VERIFY_GENERAL` have active specialist miners, whereas custom 4-field smart-contract audit schemas returned `provider_unavailable`. Failing closed to `REVIEW`/`REJECT` prevents trading agents from mistaking missing data for safety.

## Troubleshooting & Limitations

- **HTTP 401/403:** Verify `TELEGRAPH_API_KEY` is active and not expired in `/dashboard/api-keys`.
- **HTTP 402 / Insufficient Balance:** Claim Sepolia test USDC from `/dashboard/faucet` and deposit to Escrow.
- **Limitation:** Natural-language miner answers vary in formatting; `preflight_engine.py` combines JSON extraction with conservative keyword fallbacks and fails closed whenever a pillar returns `partial` or `failed`. Simulated trade execution only — never sends real on-chain swap transactions.
