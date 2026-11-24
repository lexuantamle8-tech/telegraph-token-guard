"""CLI for Telegraph Token-Risk Preflight Guard (Mission 14: Web3 Native & DeFi)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from preflight_engine import result_to_dict, run_preflight
from telegraph_client import TelegraphClient


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Telegraph Token-Risk Preflight Guard (Mission 14 — Web3 Native & DeFi)"
    )
    parser.add_argument("--chain", required=True, help="Chain (ethereum, base, arbitrum, solana, ...)")
    parser.add_argument("--address", required=True, help="Token contract or mint address")
    parser.add_argument("--symbol", required=True, help="Token ticker symbol (e.g. USDC, UNI)")
    parser.add_argument("--notional-usd", type=float, default=1000.0, help="Simulated trade size in USD")
    parser.add_argument("--max-spend-usd", type=float, default=0.15, help="Hard USD budget cap for Telegraph calls")
    parser.add_argument("--network", default="sepolia-testnet", help="Network label (e.g. sepolia-testnet)")
    parser.add_argument("--receipts", default="receipts/preflight_receipts.jsonl", help="JSONL receipt log path")
    parser.add_argument("--out", default="", help="Optional path to save full JSON verdict report")
    parser.add_argument("--dry-run", action="store_true", help="Run offline with clearly labeled dry-run fixtures")
    parser.add_argument("--dry-scenario", choices=("safe", "review", "honeypot"), default="safe")
    parser.add_argument("--unified", action="store_true", help="Use a single unified Telegraph query (faster, lower cost)")
    args = parser.parse_args()

    client = TelegraphClient(
        network=args.network,
        max_spend_usd=args.max_spend_usd,
        receipt_path=Path(args.receipts),
        dry_run=args.dry_run,
    )
    res = run_preflight(
        client=client,
        chain=args.chain,
        token_address=args.address,
        symbol=args.symbol,
        notional_usd=args.notional_usd,
        dry_run_scenario=args.dry_scenario,
        unified_query=args.unified,
    )
    payload = result_to_dict(res)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2))
    return 0 if res.verdict in ("ALLOW", "REVIEW") else 2


if __name__ == "__main__":
    sys.exit(main())
