"""
OpenAI-based log triage classifier for SRE incident response.

Classifies log entries into severity tiers (P1–P4), identifies likely root cause
categories, and suggests immediate remediation steps. Designed to complement the
ML-based anomaly detector — the anomaly detector finds *that* something is wrong,
this classifier helps the on-call engineer understand *what* and *what to do next*.
"""

import json
import os
import sys
import textwrap
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from openai import OpenAI


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class LogEntry:
    raw: str
    service: str = "unknown"
    timestamp: Optional[str] = None

    def to_prompt_block(self) -> str:
        ts = self.timestamp or datetime.now(timezone.utc).isoformat()
        return f"[{ts}] service={self.service}  {self.raw}"


@dataclass
class TriageResult:
    severity: str           # P1 | P2 | P3 | P4
    category: str           # e.g. "database", "network", "memory", "auth", "unknown"
    summary: str            # one-sentence human-readable summary
    root_cause_hint: str    # likely root cause in 1–2 sentences
    immediate_actions: list[str]   # ordered remediation steps
    escalate: bool          # True if this should page someone immediately
    confidence: float       # 0.0–1.0

    def severity_emoji(self) -> str:
        return {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🟢"}.get(self.severity, "⚪")

    def format_slack_block(self, service: str) -> str:
        actions = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(self.immediate_actions))
        escalate_note = "  → *Escalate immediately*" if self.escalate else ""
        return textwrap.dedent(f"""
            {self.severity_emoji()} *[{self.severity}] {service}* — {self.summary}
            *Category:* {self.category}
            *Root cause hint:* {self.root_cause_hint}
            *Actions:*
            {actions}{escalate_note}
            _Confidence: {self.confidence:.0%}_
        """).strip()


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert SRE triage assistant. Your job is to analyze log entries from
production services and classify them so an on-call engineer can act immediately.

Return ONLY valid JSON — no markdown, no explanation, no code fences.

JSON schema:
{
  "severity":          "P1" | "P2" | "P3" | "P4",
  "category":          string,   // one of: database | network | memory | cpu | auth | storage | timeout | config | crash | unknown
  "summary":           string,   // ≤ 15 words, plain language
  "root_cause_hint":   string,   // 1–2 sentences on likely root cause
  "immediate_actions": [string], // ordered list, 2–5 concrete steps
  "escalate":          boolean,  // true = wake someone up now
  "confidence":        number    // 0.0–1.0
}

Severity guide:
  P1 — customer-facing outage, data loss risk, security breach
  P2 — degraded service, elevated error rate > 5%, significant latency
  P3 — single component degraded, isolated errors, < 5% impact
  P4 — warning, informational anomaly, no user impact
"""

USER_PROMPT_TEMPLATE = """\
Analyze the following log entry and return a triage classification.

Log entry:
{log_block}

Service context: {service}
"""


class LogTriageClassifier:
    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        max_retries: int = 2,
    ):
        self.model = model
        self.client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self.max_retries = max_retries

    def classify(self, entry: LogEntry) -> TriageResult:
        user_msg = USER_PROMPT_TEMPLATE.format(
            log_block=entry.to_prompt_block(),
            service=entry.service,
        )

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.1,
                    max_tokens=512,
                )
                raw = response.choices[0].message.content
                data = json.loads(raw)
                return TriageResult(
                    severity=data.get("severity", "P4"),
                    category=data.get("category", "unknown"),
                    summary=data.get("summary", ""),
                    root_cause_hint=data.get("root_cause_hint", ""),
                    immediate_actions=data.get("immediate_actions", []),
                    escalate=bool(data.get("escalate", False)),
                    confidence=float(data.get("confidence", 0.0)),
                )
            except (json.JSONDecodeError, KeyError) as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"Classification failed after {self.max_retries + 1} attempts: {exc}") from exc

    def classify_batch(self, entries: list[LogEntry]) -> list[tuple[LogEntry, TriageResult]]:
        # TODO: parallelize with ThreadPoolExecutor once we understand the rate limit behavior
        results = []
        for entry in entries:
            result = self.classify(entry)
            results.append((entry, result))
        return results


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_triage_report(entry: LogEntry, result: TriageResult) -> None:
    print(f"\n{'─' * 70}")
    print(f"Service : {entry.service}")
    print(f"Log     : {entry.raw[:120]}{'…' if len(entry.raw) > 120 else ''}")
    print(f"{'─' * 70}")
    print(f"Severity: {result.severity}  ({result.severity_emoji()})")
    print(f"Category: {result.category}")
    print(f"Summary : {result.summary}")
    print(f"Escalate: {'YES — page now' if result.escalate else 'No'}")
    print(f"\nRoot cause hint:\n  {result.root_cause_hint}")
    print("\nImmediate actions:")
    for i, action in enumerate(result.immediate_actions, 1):
        print(f"  {i}. {action}")
    print(f"\nConfidence: {result.confidence:.0%}")


def export_json(results: list[tuple[LogEntry, TriageResult]], path: str) -> None:
    # TODO: support append mode for long-running pipelines instead of overwriting
    payload = [
        {
            "entry": asdict(entry),
            "triage": asdict(result),
        }
        for entry, result in results
    ]
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nResults written to {path}")


# ---------------------------------------------------------------------------
# Sample logs for smoke-testing
# ---------------------------------------------------------------------------

SAMPLE_LOGS = [
    LogEntry(
        raw='FATAL db-pool exhausted: no connections available (pool_size=50, wait_timeout=30s, pending_requests=120)',
        service="ordering-api",
        timestamp="2026-05-20T14:22:10Z",
    ),
    LogEntry(
        raw='ERROR circuit breaker OPEN for payment-service after 5 consecutive failures (latency_p99=8400ms)',
        service="checkout-service",
        timestamp="2026-05-20T14:22:15Z",
    ),
    LogEntry(
        raw='WARN memory usage at 87% (heap_used=3.4GB, heap_total=4GB), GC pressure elevated',
        service="recommendation-engine",
        timestamp="2026-05-20T14:22:30Z",
    ),
    LogEntry(
        raw='ERROR 401 Unauthorized: JWT signature verification failed for user_id=abc123 (token_age=0s)',
        service="auth-service",
        timestamp="2026-05-20T14:22:45Z",
    ),
    LogEntry(
        raw='INFO scheduled job completed: cache-warmup finished in 2.1s, 48000 keys loaded',
        service="cache-warmer",
        timestamp="2026-05-20T14:23:00Z",
    ),
]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="OpenAI-based log triage classifier",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              # Run built-in sample logs
              python ai_log_analyzer.py

              # Classify a single log line
              python ai_log_analyzer.py --log "FATAL: OOMKilled" --service my-svc

              # Read log lines from a file (one per line)
              python ai_log_analyzer.py --file /var/log/app.log --service my-svc

              # Export JSON report
              python ai_log_analyzer.py --export results.json
        """),
    )
    parser.add_argument("--log", help="Single log line to classify")
    parser.add_argument("--service", default="unknown", help="Service name")
    parser.add_argument("--file", help="Path to a log file (one entry per line)")
    parser.add_argument("--model", default="gpt-4o", help="OpenAI model (default: gpt-4o)")
    parser.add_argument("--export", metavar="PATH", help="Write results as JSON to PATH")
    args = parser.parse_args()

    if "OPENAI_API_KEY" not in os.environ:
        print("ERROR: OPENAI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    classifier = LogTriageClassifier(model=args.model)

    if args.log:
        entries = [LogEntry(raw=args.log, service=args.service)]
    elif args.file:
        with open(args.file) as fh:
            lines = [l.strip() for l in fh if l.strip()]
        entries = [LogEntry(raw=line, service=args.service) for line in lines]
    else:
        print("No input provided — running built-in sample logs.\n")
        entries = SAMPLE_LOGS

    results = classifier.classify_batch(entries)

    for entry, result in results:
        print_triage_report(entry, result)

    if args.export:
        export_json(results, args.export)

    p1_count = sum(1 for _, r in results if r.severity == "P1")
    escalate_count = sum(1 for _, r in results if r.escalate)
    print(f"\n{'─' * 70}")
    print(f"Summary: {len(results)} log(s) analyzed — {p1_count} P1, {escalate_count} require escalation")


if __name__ == "__main__":
    main()
