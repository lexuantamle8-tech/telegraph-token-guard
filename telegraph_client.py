"""Telegraph Protocol HTTP Client for Autonomous Agents.

Calls https://backend.telegraphprotocol.com/agent/chat using an Anjoman API key
(Authorization: Bearer anjoman_...), supports 202 async job polling via
GET /agent/jobs/{jobId}, enforces hard USD spending caps, and logs verifiable
receipts with SHA-256 digests.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_BASE_URL = "https://backend.telegraphprotocol.com"


class BudgetExceededError(RuntimeError):
    """Raised when a call would exceed the configured USD spend limit."""


class TelegraphAPIError(RuntimeError):
    """Raised on non-2xx HTTP responses or malformed payloads."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class TelegraphReceipt:
    timestamp_utc: str
    network: str
    mode: str  # "live" or "dry_run_fixture"
    endpoint: str
    query_kind: str
    conversation_id: Optional[str]
    job_id: Optional[str]
    message_id: Optional[str]
    status: str  # complete | partial | failed | unsupported | error
    cost_usd: Optional[float]
    latency_ms: int
    sources_count: int
    answer_sha256: str
    error: Optional[str] = None


class TelegraphClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        network: str = "sepolia-testnet",
        max_spend_usd: float = 0.15,
        timeout_sec: int = 120,
        poll_interval_sec: float = 3.0,
        max_poll_sec: float = 120.0,
        receipt_path: Optional[Path] = None,
        dry_run: bool = False,
    ) -> None:
        self.api_key = (api_key or os.environ.get("TELEGRAPH_API_KEY", "")).strip()
        self.base_url = base_url.rstrip("/")
        self.network = network
        self.max_spend_usd = float(max_spend_usd)
        self.timeout_sec = int(timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)
        self.max_poll_sec = float(max_poll_sec)
        self.receipt_path = receipt_path
        self.dry_run = dry_run
        self.session_spend_usd = 0.0
        self.receipts: List[TelegraphReceipt] = []

        if not self.dry_run and not self.api_key:
            raise ValueError(
                "TELEGRAPH_API_KEY is required for live mode. "
                "Create one at https://anjoman.ai/dashboard/api-keys or pass --dry-run."
            )

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "telegraph-token-guard/1.0",
        }

    def check_health(self) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"User-Agent": "telegraph-token-guard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_json(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> tuple[int, Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise TelegraphAPIError(
                f"Telegraph HTTP {exc.code} on {path}: {body[:240]}",
                status_code=exc.code,
                body=body,
            ) from exc
        except urllib.error.URLError as exc:
            raise TelegraphAPIError(f"Network error calling {url}: {exc}") from exc

    def _record_receipt(self, receipt: TelegraphReceipt) -> None:
        self.receipts.append(receipt)
        if self.receipt_path:
            self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
            with self.receipt_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(receipt)) + "\n")

    def _http_sse_chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Call POST /agent/chat using SSE streaming so Cloudflare keep-alives prevent 504s."""
        url = f"{self.base_url}/agent/chat"
        data = json.dumps(payload).encode("utf-8")
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        conv_id: Optional[str] = None
        job_id: Optional[str] = None
        message_id: Optional[str] = None
        tokens: List[str] = []
        done_payload: Dict[str, Any] = {}
        traces: List[Any] = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if "application/json" in ctype:
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
                event_name = ""
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        raw_data = line[5:].strip()
                        try:
                            obj = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue
                        if event_name == "conversation":
                            conv_id = obj.get("conversationId") or conv_id
                        elif event_name == "job":
                            job_id = obj.get("jobId") or job_id
                        elif event_name == "token":
                            tokens.append(str(obj.get("text") or ""))
                        elif event_name == "trace":
                            traces.append(obj)
                        elif event_name == "done":
                            done_payload = obj
                            message_id = obj.get("messageId") or message_id
                        elif event_name == "error":
                            raise TelegraphAPIError(
                                f"Telegraph SSE error: {obj.get('message') or obj}",
                                status_code=500,
                            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise TelegraphAPIError(
                f"Telegraph HTTP {exc.code} on /agent/chat: {body[:240]}",
                status_code=exc.code,
                body=body,
            ) from exc
        except urllib.error.URLError as exc:
            raise TelegraphAPIError(f"Network error calling {url}: {exc}") from exc

        answer = done_payload.get("answer")
        if answer is None:
            answer = "".join(tokens)
        result: Dict[str, Any] = {
            "conversationId": done_payload.get("conversationId") or conv_id,
            "jobId": done_payload.get("jobId") or job_id,
            "messageId": message_id,
            "answer": answer,
            "status": done_payload.get("status") or ("complete" if answer else "failed"),
            "sources": done_payload.get("sources") or [],
            "files": done_payload.get("files") or [],
            "costUsd": done_payload.get("costUsd"),
            "traces": traces,
        }
        # If costUsd wasn't in done event, fetch final job metadata once to get exact costUsd
        if result["costUsd"] is None and result["jobId"]:
            try:
                _, job_info = self._http_json("GET", f"/agent/jobs/{result['jobId']}")
                if job_info.get("costUsd") is not None:
                    result["costUsd"] = job_info.get("costUsd")
                if not result["sources"] and job_info.get("sources"):
                    result["sources"] = job_info.get("sources")
            except TelegraphAPIError:
                pass
        return result

    def ask(
        self,
        message: str,
        query_kind: str = "general",
        conversation_id: Optional[str] = None,
        estimated_cost_usd: float = 0.015,
        dry_run_response: Optional[Dict[str, Any]] = None,
        use_stream: bool = True,
    ) -> Dict[str, Any]:
        if self.session_spend_usd + estimated_cost_usd > self.max_spend_usd + 1e-9:
            raise BudgetExceededError(
                f"Blocked Telegraph call ({query_kind}): session spend "
                f"${self.session_spend_usd:.4f} + est ${estimated_cost_usd:.4f} "
                f"> max_spend_usd ${self.max_spend_usd:.4f}"
            )

        t0 = time.monotonic()
        if self.dry_run:
            reply = dry_run_response or {
                "conversationId": conversation_id or "dryrun-conv-001",
                "jobId": f"dryrun-job-{query_kind}",
                "messageId": f"dryrun-msg-{query_kind}",
                "answer": "Dry-run fixture response.",
                "status": "complete",
                "sources": [{"title": "Dry-run fixture", "url": "https://anjoman.ai/docs"}],
                "files": [],
                "costUsd": 0.0,
            }
            latency_ms = int((time.monotonic() - t0) * 1000)
            answer_text = str(reply.get("answer") or "")
            receipt = TelegraphReceipt(
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                network=self.network,
                mode="dry_run_fixture",
                endpoint="/agent/chat",
                query_kind=query_kind,
                conversation_id=reply.get("conversationId"),
                job_id=reply.get("jobId"),
                message_id=reply.get("messageId"),
                status=str(reply.get("status") or "complete"),
                cost_usd=reply.get("costUsd"),
                latency_ms=latency_ms,
                sources_count=len(reply.get("sources") or []),
                answer_sha256=hashlib.sha256(answer_text.encode("utf-8")).hexdigest(),
            )
            self._record_receipt(receipt)
            return reply

        if use_stream:
            body: Dict[str, Any] = {"message": message}
            if conversation_id:
                body["conversationId"] = conversation_id
            reply = self._http_sse_chat(body)
        else:
            body = {"message": message, "stream": False}
            if conversation_id:
                body["conversationId"] = conversation_id

            status_code, reply = self._http_json("POST", "/agent/chat", body)

            # Handle 202 async job polling per Telegraph API docs
            if status_code == 202 or reply.get("status") == "running":
                job_id = reply.get("jobId")
                if not job_id:
                    raise TelegraphAPIError("Telegraph returned 202/running without jobId")
                deadline = time.monotonic() + self.max_poll_sec
                while time.monotonic() < deadline:
                    time.sleep(self.poll_interval_sec)
                    _, job = self._http_json("GET", f"/agent/jobs/{job_id}")
                    if job.get("status") != "running":
                        reply = job
                        break
                else:
                    raise TelegraphAPIError(f"Telegraph job {job_id} timed out after {self.max_poll_sec}s")

        latency_ms = int((time.monotonic() - t0) * 1000)
        cost = reply.get("costUsd")
        if isinstance(cost, (int, float)):
            self.session_spend_usd += float(cost)
            if self.session_spend_usd > self.max_spend_usd + 1e-9:
                # Record receipt first so spend is never hidden, then enforce budget on subsequent calls
                pass

        answer_text = str(reply.get("answer") or "")
        receipt = TelegraphReceipt(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            network=self.network,
            mode="live",
            endpoint="/agent/chat",
            query_kind=query_kind,
            conversation_id=reply.get("conversationId"),
            job_id=reply.get("jobId"),
            message_id=reply.get("messageId"),
            status=str(reply.get("status") or "unknown"),
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            latency_ms=latency_ms,
            sources_count=len(reply.get("sources") or []),
            answer_sha256=hashlib.sha256(answer_text.encode("utf-8")).hexdigest(),
        )
        self._record_receipt(receipt)
        return reply
