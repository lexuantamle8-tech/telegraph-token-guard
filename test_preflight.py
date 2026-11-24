"""Unit & Failure-Case Tests for Telegraph Token-Risk Preflight Guard."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from preflight_engine import run_preflight
from telegraph_client import TelegraphAPIError, TelegraphClient


class TestPreflightGuard(unittest.TestCase):
    def test_safe_token_allows_trade_and_writes_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rpath = Path(td) / "receipts.jsonl"
            client = TelegraphClient(dry_run=True, receipt_path=rpath, max_spend_usd=0.10)
            res = run_preflight(
                client,
                chain="ethereum",
                token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                symbol="USDC",
                notional_usd=2500.0,
                dry_run_scenario="safe",
            )
            self.assertEqual(res.verdict, "ALLOW")
            self.assertLess(res.risk_score, 25)
            self.assertEqual(len(res.job_ids), 3)
            lines = rpath.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)
            first = json.loads(lines[0])
            self.assertEqual(first["mode"], "dry_run_fixture")
            self.assertEqual(len(first["answer_sha256"]), 64)

    def test_honeypot_token_hard_rejects(self) -> None:
        client = TelegraphClient(dry_run=True, max_spend_usd=0.10)
        res = run_preflight(
            client,
            chain="base",
            token_address="0x1111111111111111111111111111111111111111",
            symbol="SCAMX",
            notional_usd=1000.0,
            dry_run_scenario="honeypot",
        )
        self.assertEqual(res.verdict, "REJECT")
        self.assertGreaterEqual(res.risk_score, 60)
        self.assertTrue(any("Honeypot" in r for r in res.reasons))

    def test_medium_risk_holds_for_review(self) -> None:
        client = TelegraphClient(dry_run=True, max_spend_usd=0.10)
        res = run_preflight(
            client,
            chain="arbitrum",
            token_address="0x2222222222222222222222222222222222222222",
            symbol="NEWDEFI",
            notional_usd=5000.0,
            dry_run_scenario="review",
        )
        self.assertEqual(res.verdict, "REVIEW")
        self.assertGreaterEqual(res.risk_score, 25)

    def test_invalid_address_rejected_with_zero_spend(self) -> None:
        client = TelegraphClient(dry_run=True, max_spend_usd=0.10)
        res = run_preflight(
            client,
            chain="ethereum",
            token_address="not-a-valid-address",
            symbol="BAD",
            notional_usd=500.0,
        )
        self.assertEqual(res.verdict, "REJECT")
        self.assertEqual(res.total_intelligence_cost_usd, 0.0)
        self.assertEqual(len(client.receipts), 0)
        self.assertEqual(res.simulated_trade_action, "BLOCKED_BEFORE_INTELLIGENCE_PURCHASE")

    def test_spending_cap_blocks_excess_calls_and_fails_closed(self) -> None:
        client = TelegraphClient(dry_run=True, max_spend_usd=0.01)
        res = run_preflight(
            client,
            chain="solana",
            token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            symbol="USDC",
            notional_usd=1000.0,
        )
        # Budget 0.01 < estimated 0.015 per call -> all 3 pillars blocked, fails closed to REJECT
        self.assertEqual(res.verdict, "REJECT")
        self.assertTrue(all(v == "budget_blocked" for v in res.pillar_statuses.values()))

    def test_partial_or_api_error_fails_closed(self) -> None:
        client = TelegraphClient(api_key="anjoman_test_dummy", max_spend_usd=0.15)
        with patch.object(
            client,
            "_http_sse_chat",
            side_effect=[
                {"status": "partial", "jobId": "j1", "answer": "partial audit", "costUsd": 0.01, "sources": []},
                TelegraphAPIError("HTTP 402 Insufficient balance", status_code=402),
                {"status": "complete", "jobId": "j3", "answer": '{"liquidity_usd": 5000000, "contract_age_days": 200}', "costUsd": 0.01, "sources": []},
            ],
        ):
            res = run_preflight(
                client,
                chain="ethereum",
                token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                symbol="USDC",
                notional_usd=1000.0,
            )
            self.assertEqual(res.verdict, "REJECT")
            self.assertEqual(res.pillar_statuses["SMART_CONTRACT_AUDIT"], "partial")
            self.assertEqual(res.pillar_statuses["WASH_TRADING_DETECTION"], "api_error")
            self.assertAlmostEqual(res.total_intelligence_cost_usd, 0.02, places=6)

    def test_async_202_job_polling_succeeds(self) -> None:
        client = TelegraphClient(
            api_key="anjoman_test_dummy", max_spend_usd=0.15, poll_interval_sec=0.01
        )
        with patch.object(
            client,
            "_http_json",
            side_effect=[
                (202, {"status": "running", "jobId": "job-async-1", "conversationId": "conv-1"}),
                (200, {"status": "complete", "jobId": "job-async-1", "answer": '{"honeypot": false, "sell_tax_pct": 0}', "costUsd": 0.012, "sources": [{"url": "https://etherscan.io"}]}),
            ],
        ):
            reply = client.ask("audit test", query_kind="SMART_CONTRACT_AUDIT", use_stream=False)
            self.assertEqual(reply["status"], "complete")
            self.assertEqual(reply["jobId"], "job-async-1")
            self.assertAlmostEqual(client.session_spend_usd, 0.012, places=6)


if __name__ == "__main__":
    unittest.main()
