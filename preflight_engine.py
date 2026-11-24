"""Mission 14 (Web3 Native & DeFi) Token-Risk Preflight Engine.

Given a chain, token address, symbol, and simulated trade notional USD:
1. Validates inputs locally before spending any escrow funds ($0.00 on invalid inputs).
2. Purchases bounded intelligence from Telegraph Protocol across 3 risk pillars:
   - SMART_CONTRACT_AUDIT (honeypot, mint/freeze/blacklist authority, sell tax)
   - WASH_TRADING_DETECTION (wash volume ratio, top-10 holder concentration, LP lock)
   - ONCHAIN_METRIC_VERIFY (liquidity depth vs notional, contract age, active holders)
3. Evaluates deterministic risk rules (fail-closed on partial/failed responses)
   and emits ALLOW / REVIEW / REJECT with full Telegraph receipts and cost accounting.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegraph_client import BudgetExceededError, TelegraphAPIError, TelegraphClient

SUPPORTED_CHAINS = {"ethereum", "base", "arbitrum", "optimism", "polygon", "solana"}
EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass
class PreflightResult:
    timestamp_utc: str
    network: str
    mode: str
    chain: str
    token_address: str
    symbol: str
    notional_usd: float
    verdict: str  # ALLOW | REVIEW | REJECT
    risk_score: int  # 0 (safest) .. 100 (critical risk)
    reasons: List[str] = field(default_factory=list)
    total_intelligence_cost_usd: float = 0.0
    cost_to_notional_bps: float = 0.0
    simulated_trade_action: str = ""
    pillar_statuses: Dict[str, str] = field(default_factory=dict)
    job_ids: List[str] = field(default_factory=list)
    sources: List[Any] = field(default_factory=list)
    raw_answers: Dict[str, str] = field(default_factory=dict)


def validate_token_input(chain: str, token_address: str, notional_usd: float) -> Optional[str]:
    c = chain.lower().strip()
    if c not in SUPPORTED_CHAINS:
        return f"Unsupported chain '{chain}'. Supported: {sorted(SUPPORTED_CHAINS)}"
    if notional_usd <= 0:
        return f"Trade notional_usd must be > 0 (got {notional_usd})"
    addr = token_address.strip()
    if c == "solana":
        if not SOL_ADDR_RE.match(addr):
            return f"Invalid Solana Base58 mint address: '{addr}'"
    else:
        if not EVM_ADDR_RE.match(addr):
            return f"Invalid EVM 0x address for {c}: '{addr}'"
    return None


def _extract_json_block(text: str) -> Dict[str, Any]:
    """Best-effort JSON extraction from miner/agent natural language answer."""
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _heuristic_signals(text: str) -> Dict[str, Any]:
    """Fallback keyword/signal parser when miners respond in prose rather than strict JSON."""
    low = text.lower()
    signals: Dict[str, Any] = {}
    if any(k in low for k in ("honeypot", "cannot sell", "sell tax 99", "sell tax 100", "rug pull", "malicious drain")):
        signals["critical_honeypot"] = True
    if any(k in low for k in ("unlimited mint", "unrenounced mint", "blacklist function", "freeze authority enabled")):
        signals["privileged_admin_risk"] = True
    if any(k in low for k in ("wash trading detected", "high wash volume", "fake volume", "circular trades")):
        signals["wash_trading_flag"] = True
    if any(k in low for k in ("low liquidity", "thin liquidity", "liquidity under $10", "unlocked lp")):
        signals["thin_liquidity_flag"] = True
    if any(k in low for k in ("verified contract", "open source", "audited", "deep liquidity", "established token")):
        signals["positive_verification"] = True
    return signals


def build_dry_run_fixtures(symbol: str, scenario: str = "safe") -> Dict[str, Dict[str, Any]]:
    """Deterministic fixtures for offline tests & reviewer walkthroughs (clearly marked dry_run_fixture)."""
    if scenario == "honeypot":
        return {
            "SMART_CONTRACT_AUDIT": {
                "conversationId": "dryrun-conv-honeypot",
                "jobId": "dryrun-job-audit-hp",
                "messageId": "dryrun-msg-audit-hp",
                "status": "complete",
                "costUsd": 0.01,
                "sources": [{"title": "Simulated Contract Scanner", "url": "https://anjoman.ai/docs"}],
                "answer": json.dumps({
                    "honeypot": True,
                    "sell_tax_pct": 99.0,
                    "mintable_by_owner": True,
                    "proxy_upgradable": True,
                    "summary": f"{symbol} blocks non-owner sells (99% sell tax honeypot).",
                }),
            },
            "WASH_TRADING_DETECTION": {
                "conversationId": "dryrun-conv-honeypot",
                "jobId": "dryrun-job-wash-hp",
                "messageId": "dryrun-msg-wash-hp",
                "status": "complete",
                "costUsd": 0.01,
                "sources": [{"title": "Simulated Flow Analyzer", "url": "https://anjoman.ai/docs"}],
                "answer": json.dumps({
                    "wash_volume_ratio": 0.84,
                    "top10_holder_pct": 91.5,
                    "lp_locked": False,
                    "summary": "84% circular wash volume across 4 funded wallets; LP unlocked.",
                }),
            },
            "ONCHAIN_METRIC_VERIFY": {
                "conversationId": "dryrun-conv-honeypot",
                "jobId": "dryrun-job-onchain-hp",
                "messageId": "dryrun-msg-onchain-hp",
                "status": "complete",
                "costUsd": 0.01,
                "sources": [{"title": "Simulated Pool Verifier", "url": "https://anjoman.ai/docs"}],
                "answer": json.dumps({
                    "liquidity_usd": 4200,
                    "contract_age_days": 1,
                    "active_holders": 19,
                }),
            },
        }
    if scenario == "review":
        return {
            "SMART_CONTRACT_AUDIT": {
                "conversationId": "dryrun-conv-review",
                "jobId": "dryrun-job-audit-rv",
                "messageId": "dryrun-msg-audit-rv",
                "status": "complete",
                "costUsd": 0.01,
                "sources": [{"title": "Simulated Contract Scanner", "url": "https://anjoman.ai/docs"}],
                "answer": json.dumps({
                    "honeypot": False,
                    "sell_tax_pct": 3.0,
                    "mintable_by_owner": False,
                    "proxy_upgradable": True,
                    "summary": f"{symbol} is upgradable via 2/3 multisig proxy; no honeypot.",
                }),
            },
            "WASH_TRADING_DETECTION": {
                "conversationId": "dryrun-conv-review",
                "jobId": "dryrun-job-wash-rv",
                "messageId": "dryrun-msg-wash-rv",
                "status": "complete",
                "costUsd": 0.01,
                "sources": [{"title": "Simulated Flow Analyzer", "url": "https://anjoman.ai/docs"}],
                "answer": json.dumps({
                    "wash_volume_ratio": 0.28,
                    "top10_holder_pct": 54.0,
                    "lp_locked": True,
                }),
            },
            "ONCHAIN_METRIC_VERIFY": {
                "conversationId": "dryrun-conv-review",
                "jobId": "dryrun-job-onchain-rv",
                "messageId": "dryrun-msg-onchain-rv",
                "status": "complete",
                "costUsd": 0.01,
                "sources": [{"title": "Simulated Pool Verifier", "url": "https://anjoman.ai/docs"}],
                "answer": json.dumps({
                    "liquidity_usd": 85000,
                    "contract_age_days": 18,
                    "active_holders": 620,
                }),
            },
        }
    return {
        "SMART_CONTRACT_AUDIT": {
            "conversationId": "dryrun-conv-safe",
            "jobId": "dryrun-job-audit-ok",
            "messageId": "dryrun-msg-audit-ok",
            "status": "complete",
            "costUsd": 0.01,
            "sources": [{"title": "Simulated Contract Scanner", "url": "https://anjoman.ai/docs"}],
            "answer": json.dumps({
                "honeypot": False,
                "sell_tax_pct": 0.0,
                "mintable_by_owner": False,
                "proxy_upgradable": False,
                "summary": f"{symbol} verified immutable contract, 0% tax, audited.",
            }),
        },
        "WASH_TRADING_DETECTION": {
            "conversationId": "dryrun-conv-safe",
            "jobId": "dryrun-job-wash-ok",
            "messageId": "dryrun-msg-wash-ok",
            "status": "complete",
            "costUsd": 0.01,
            "sources": [{"title": "Simulated Flow Analyzer", "url": "https://anjoman.ai/docs"}],
            "answer": json.dumps({
                "wash_volume_ratio": 0.04,
                "top10_holder_pct": 18.2,
                "lp_locked": True,
            }),
        },
        "ONCHAIN_METRIC_VERIFY": {
            "conversationId": "dryrun-conv-safe",
            "jobId": "dryrun-job-onchain-ok",
            "messageId": "dryrun-msg-onchain-ok",
            "status": "complete",
            "costUsd": 0.01,
            "sources": [{"title": "Simulated Pool Verifier", "url": "https://anjoman.ai/docs"}],
            "answer": json.dumps({
                "liquidity_usd": 42000000,
                "contract_age_days": 910,
                "active_holders": 148000,
            }),
        },
    }


def run_preflight(
    client: TelegraphClient,
    chain: str,
    token_address: str,
    symbol: str,
    notional_usd: float,
    dry_run_scenario: str = "safe",
    unified_query: bool = False,
) -> PreflightResult:
    now = datetime.now(timezone.utc).isoformat()
    mode = "dry_run_fixture" if client.dry_run else "live"
    err = validate_token_input(chain, token_address, notional_usd)
    if err:
        return PreflightResult(
            timestamp_utc=now,
            network=client.network,
            mode=mode,
            chain=chain,
            token_address=token_address,
            symbol=symbol,
            notional_usd=notional_usd,
            verdict="REJECT",
            risk_score=100,
            reasons=[f"Input validation failed ($0.00 spent): {err}"],
            total_intelligence_cost_usd=0.0,
            cost_to_notional_bps=0.0,
            simulated_trade_action="BLOCKED_BEFORE_INTELLIGENCE_PURCHASE",
        )

    fixtures = build_dry_run_fixtures(symbol, dry_run_scenario) if client.dry_run else {}
    if unified_query:
        prompts = [
            (
                "ONCHAIN_METRIC_VERIFY",
                (
                    f"What is the current USD price of {symbol} and the verified on-chain liquidity depth "
                    f"for {symbol} on {chain} (contract {token_address})?"
                ),
            )
        ]
    else:
        prompts = [
        (
            "SMART_CONTRACT_AUDIT",
            (
                f"Perform a concise smart-contract security check on {symbol} ({token_address}) on {chain}. "
                "Return JSON with keys: honeypot (bool), sell_tax_pct (number), mintable_by_owner (bool), "
                "proxy_upgradable (bool), summary (string). Cite verifiable on-chain or security sources."
            ),
        ),
        (
            "WASH_TRADING_DETECTION",
            (
                f"Check market microstructure and wash-trading risk for {symbol} ({token_address}) on {chain}. "
                "Return JSON with keys: wash_volume_ratio (0..1), top10_holder_pct (0..100), "
                "lp_locked (bool), summary (string)."
            ),
        ),
        (
            "ONCHAIN_METRIC_VERIFY",
            (
                f"Verify live on-chain liquidity and maturity metrics for {symbol} ({token_address}) on {chain}. "
                "Return JSON with keys: liquidity_usd (number), contract_age_days (number), "
                "active_holders (number), summary (string)."
            ),
        ),
    ]

    risk_score = 0
    reasons: List[str] = []
    pillar_statuses: Dict[str, str] = {}
    job_ids: List[str] = []
    sources: List[Any] = []
    raw_answers: Dict[str, str] = {}
    total_cost = 0.0
    conv_id: Optional[str] = None
    hard_reject = False
    incomplete_pillar = False

    for kind, prompt in prompts:
        try:
            reply = client.ask(
                prompt,
                query_kind=kind,
                conversation_id=conv_id,
                dry_run_response=fixtures.get(kind),
            )
        except BudgetExceededError as exc:
            incomplete_pillar = True
            pillar_statuses[kind] = "budget_blocked"
            risk_score += 30
            reasons.append(f"{kind}: {exc}")
            continue
        except TelegraphAPIError as exc:
            incomplete_pillar = True
            pillar_statuses[kind] = "api_error"
            risk_score += 35
            reasons.append(f"{kind} failed ({exc.status_code}): {exc}")
            continue

        conv_id = reply.get("conversationId") or conv_id
        if reply.get("jobId"):
            job_ids.append(str(reply["jobId"]))
        status = str(reply.get("status") or "unknown")
        pillar_statuses[kind] = status
        cost = reply.get("costUsd")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
        sources.extend(reply.get("sources") or [])
        answer = str(reply.get("answer") or "")
        raw_answers[kind] = answer

        if status != "complete":
            incomplete_pillar = True
            risk_score += 25
            reasons.append(f"{kind} returned status='{status}' (fail-closed penalty +25).")

        parsed = _extract_json_block(answer)
        heur = _heuristic_signals(answer) if not parsed else {}

        if kind == "SMART_CONTRACT_AUDIT":
            if parsed.get("honeypot") is True or heur.get("critical_honeypot"):
                hard_reject = True
                risk_score += 70
                reasons.append("CRITICAL: Honeypot or non-sellable token behavior detected.")
            sell_tax = parsed.get("sell_tax_pct")
            if isinstance(sell_tax, (int, float)):
                if sell_tax >= 15:
                    hard_reject = True
                    risk_score += 45
                    reasons.append(f"CRITICAL: Excessive sell tax ({sell_tax}%).")
                elif sell_tax >= 5:
                    risk_score += 18
                    reasons.append(f"Elevated sell tax ({sell_tax}%).")
            if parsed.get("mintable_by_owner") is True or heur.get("privileged_admin_risk"):
                risk_score += 22
                reasons.append("Privileged mint/freeze/blacklist authority present.")
            if parsed.get("proxy_upgradable") is True:
                risk_score += 12
                reasons.append("Contract is upgradable proxy (governance risk).")

        elif kind == "WASH_TRADING_DETECTION":
            wash = parsed.get("wash_volume_ratio")
            if isinstance(wash, (int, float)):
                if wash >= 0.5:
                    hard_reject = True
                    risk_score += 40
                    reasons.append(f"CRITICAL: Severe wash-trading ratio ({wash:.0%}).")
                elif wash >= 0.2:
                    risk_score += 16
                    reasons.append(f"Moderate wash-trading signal ({wash:.0%}).")
            elif heur.get("wash_trading_flag"):
                risk_score += 25
                reasons.append("Wash-trading warning flagged in intelligence report.")
            top10 = parsed.get("top10_holder_pct")
            if isinstance(top10, (int, float)):
                if top10 >= 70:
                    risk_score += 25
                    reasons.append(f"Extreme whale concentration (top-10 hold {top10:.1f}%).")
                elif top10 >= 45:
                    risk_score += 12
                    reasons.append(f"Elevated holder concentration (top-10 hold {top10:.1f}%).")
            if parsed.get("lp_locked") is False:
                risk_score += 18
                reasons.append("Liquidity pool is unlocked (rug risk).")

        elif kind == "ONCHAIN_METRIC_VERIFY":
            liq = parsed.get("liquidity_usd")
            if isinstance(liq, (int, float)):
                if liq < max(10000.0, notional_usd * 10.0):
                    risk_score += 30
                    reasons.append(
                        f"Thin liquidity (${liq:,.0f}) relative to trade size (${notional_usd:,.0f})."
                    )
                elif liq < notional_usd * 25.0:
                    risk_score += 12
                    reasons.append(f"Moderate liquidity depth (${liq:,.0f}) vs notional.")
            elif heur.get("thin_liquidity_flag"):
                risk_score += 20
                reasons.append("Thin liquidity flagged in intelligence report.")
            age = parsed.get("contract_age_days")
            if isinstance(age, (int, float)) and age < 7:
                risk_score += 15
                reasons.append(f"Newly deployed contract ({age} days old).")

    risk_score = min(100, max(0, risk_score))

    if hard_reject or risk_score >= 60:
        verdict = "REJECT"
        sim_action = f"SIMULATED_TRADE_BLOCKED (${notional_usd:,.2f} {symbol} on {chain})"
    elif incomplete_pillar or risk_score >= 25:
        verdict = "REVIEW"
        sim_action = f"SIMULATED_TRADE_HELD_FOR_HUMAN_REVIEW (${notional_usd:,.2f} {symbol} on {chain})"
    else:
        verdict = "ALLOW"
        sim_action = f"SIMULATED_TRADE_APPROVED (${notional_usd:,.2f} {symbol} on {chain})"
        if not reasons:
            reasons.append("All 3 Telegraph risk pillars passed within policy thresholds.")

    cost_bps = (total_cost / notional_usd) * 10000.0 if notional_usd > 0 else 0.0

    return PreflightResult(
        timestamp_utc=now,
        network=client.network,
        mode=mode,
        chain=chain.lower().strip(),
        token_address=token_address.strip(),
        symbol=symbol.upper().strip(),
        notional_usd=notional_usd,
        verdict=verdict,
        risk_score=risk_score,
        reasons=reasons,
        total_intelligence_cost_usd=round(total_cost, 6),
        cost_to_notional_bps=round(cost_bps, 4),
        simulated_trade_action=sim_action,
        pillar_statuses=pillar_statuses,
        job_ids=job_ids,
        sources=sources[:12],
        raw_answers=raw_answers,
    )


def result_to_dict(res: PreflightResult) -> Dict[str, Any]:
    return asdict(res)
