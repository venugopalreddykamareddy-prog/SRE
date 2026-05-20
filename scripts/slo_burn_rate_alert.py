#!/usr/bin/env python3
"""
Multi-window SLO burn rate alerting.

Implements the Google SRE book's multi-window burn rate approach:
  - 1h  window burn rate > 14.4x  → P1 (fast burn, page immediately)
  - 6h  window burn rate > 6x     → P2 (medium burn, page)
  - 72h window burn rate > 3x AND 6h > 1x → P3 (slow burn, ticket)

Burn rate = observed_error_rate / error_budget_rate
Monthly error budget rate for 99.9% SLO = 0.001 (sustained over 30 days).
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests
import yaml


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SLOConfig:
    service_name: str
    slo_target: float                  # e.g. 0.999
    metric_query_errors: str           # Datadog metric query for error count
    metric_query_total: str            # Datadog metric query for total requests
    pagerduty_service_key: str


def load_configs(config_path: str, service_filter: Optional[str] = None) -> list[SLOConfig]:
    with open(config_path) as fh:
        raw = yaml.safe_load(fh)
    configs = [SLOConfig(**svc) for svc in raw.get("services", [])]
    if service_filter:
        configs = [c for c in configs if c.service_name == service_filter]
    return configs


# ---------------------------------------------------------------------------
# Datadog client
# ---------------------------------------------------------------------------

class DatadogClient:
    BASE = "https://api.datadoghq.com/api/v1"

    def __init__(self) -> None:
        self._headers = {
            "DD-API-KEY": os.environ["DD_API_KEY"],
            "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        }

    def error_rate(self, errors_query: str, total_query: str, window_seconds: int) -> float:
        now = int(time.time())
        from_ts = now - window_seconds

        errors = self._sum(errors_query, from_ts, now)
        total = self._sum(total_query, from_ts, now)
        if total == 0:
            return 0.0
        return errors / total

    def _sum(self, query: str, from_ts: int, to_ts: int) -> float:
        resp = requests.get(
            f"{self.BASE}/query",
            headers=self._headers,
            params={"query": query, "from": from_ts, "to": to_ts},
            timeout=15,
        )
        resp.raise_for_status()
        series = resp.json().get("series", [])
        if not series or not series[0].get("pointlist"):
            return 0.0
        # TODO: this assumes counter metrics; summing gauge points (e.g. latency) will give wrong results
        return sum(p[1] for p in series[0]["pointlist"] if p[1] is not None)


# ---------------------------------------------------------------------------
# Burn rate calculator
# ---------------------------------------------------------------------------

@dataclass
class BurnRateResult:
    window_label: str
    window_seconds: int
    error_rate: float
    burn_rate: float
    error_budget_remaining_pct: float


def calculate_burn_rate(error_rate: float, slo_target: float, window_label: str, window_seconds: int) -> BurnRateResult:
    budget_rate = 1.0 - slo_target
    burn_rate = (error_rate / budget_rate) if budget_rate > 0 else 0.0
    # Remaining budget as % of monthly, approximated from observed burn over the window
    monthly_seconds = 30 * 24 * 3600
    budget_consumed_pct = (burn_rate * window_seconds / monthly_seconds) * 100
    return BurnRateResult(
        window_label=window_label,
        window_seconds=window_seconds,
        error_rate=round(error_rate, 6),
        burn_rate=round(burn_rate, 3),
        error_budget_remaining_pct=round(max(0.0, 100.0 - budget_consumed_pct), 4),
    )


# ---------------------------------------------------------------------------
# Multi-window evaluation
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    severity: str         # P1 | P2 | P3 | None
    description: str
    windows: list[BurnRateResult]


_FAST_BURN_THRESHOLD = 14.4    # 1h window
_MEDIUM_BURN_THRESHOLD = 6.0   # 6h window
_SLOW_BURN_LONG_THRESHOLD = 3.0   # 72h window
_SLOW_BURN_SHORT_THRESHOLD = 1.0  # 6h window (secondary check for slow burn)


def evaluate_burn_rates(
    dd: DatadogClient, cfg: SLOConfig
) -> tuple[list[BurnRateResult], Optional[Alert]]:
    windows = [
        ("1h",  3_600),
        ("6h",  21_600),
        ("72h", 259_200),
    ]
    results = []
    for label, seconds in windows:
        rate = dd.error_rate(cfg.metric_query_errors, cfg.metric_query_total, seconds)
        results.append(calculate_burn_rate(rate, cfg.slo_target, label, seconds))

    by_label = {r.window_label: r for r in results}
    r1h = by_label["1h"]
    r6h = by_label["6h"]
    r72h = by_label["72h"]

    alert: Optional[Alert] = None

    if r1h.burn_rate > _FAST_BURN_THRESHOLD:
        alert = Alert(
            severity="P1",
            description=(
                f"Fast burn detected on {cfg.service_name}: "
                f"1h burn rate {r1h.burn_rate:.1f}x (threshold {_FAST_BURN_THRESHOLD}x). "
                f"At this rate the monthly error budget will be exhausted in "
                f"{_hours_until_exhaustion(r1h.burn_rate):.1f} hours."
            ),
            windows=results,
        )
    elif r6h.burn_rate > _MEDIUM_BURN_THRESHOLD:
        alert = Alert(
            severity="P2",
            description=(
                f"Medium burn detected on {cfg.service_name}: "
                f"6h burn rate {r6h.burn_rate:.1f}x (threshold {_MEDIUM_BURN_THRESHOLD}x)."
            ),
            windows=results,
        )
    elif r72h.burn_rate > _SLOW_BURN_LONG_THRESHOLD and r6h.burn_rate > _SLOW_BURN_SHORT_THRESHOLD:
        alert = Alert(
            severity="P3",
            description=(
                f"Slow burn detected on {cfg.service_name}: "
                f"72h burn rate {r72h.burn_rate:.1f}x with 6h burn rate {r6h.burn_rate:.1f}x."
            ),
            windows=results,
        )

    return results, alert


def _hours_until_exhaustion(burn_rate: float) -> float:
    # Monthly budget at 1x burn = 30 * 24 = 720 hours. At Nx burn = 720/N hours.
    return 720.0 / burn_rate if burn_rate > 0 else float("inf")


# ---------------------------------------------------------------------------
# PagerDuty client
# ---------------------------------------------------------------------------

class PagerDutyClient:
    EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

    def __init__(self, routing_key: str) -> None:
        self._key = routing_key

    def trigger(self, service: str, alert: Alert, dry_run: bool = False) -> None:
        severity_map = {"P1": "critical", "P2": "error", "P3": "warning"}
        payload = {
            "routing_key": self._key,
            "event_action": "trigger",
            # TODO: include severity in dedup_key — right now a P2 will suppress a P1 escalation for the same service
            "dedup_key": f"{service}-slo-burn",
            "payload": {
                "summary": alert.description,
                "severity": severity_map.get(alert.severity, "warning"),
                "source": service,
                "custom_details": {
                    w.window_label: {
                        "error_rate": w.error_rate,
                        "burn_rate": f"{w.burn_rate}x",
                        "budget_remaining_pct": w.error_budget_remaining_pct,
                    }
                    for w in alert.windows
                },
            },
        }
        if dry_run:
            print(f"  [dry-run] Would page PagerDuty: {alert.severity} — {alert.description}")
            return
        resp = requests.post(self.EVENTS_URL, json=payload, timeout=10)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_service_report(cfg: SLOConfig, windows: list[BurnRateResult], alert: Optional[Alert]) -> None:
    print(f"\n{'─' * 60}")
    print(f"Service: {cfg.service_name}  (SLO target: {cfg.slo_target * 100:.3f}%)")
    for w in windows:
        flag = ""
        if w.window_label == "1h" and w.burn_rate > _FAST_BURN_THRESHOLD:
            flag = "  ← FAST BURN"
        elif w.window_label == "6h" and w.burn_rate > _MEDIUM_BURN_THRESHOLD:
            flag = "  ← MEDIUM BURN"
        elif w.window_label == "72h" and w.burn_rate > _SLOW_BURN_LONG_THRESHOLD:
            flag = "  ← SLOW BURN"
        print(f"  {w.window_label:>4}  error_rate={w.error_rate:.6f}  burn={w.burn_rate:>6.2f}x  budget_remaining={w.error_budget_remaining_pct:.2f}%{flag}")
    if alert:
        print(f"\n  [{alert.severity}] {alert.description}")
    else:
        print(f"\n  [OK] All burn rates within threshold")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-window SLO burn rate alerting")
    parser.add_argument("--config", default="scripts/slo-config.yaml", help="Path to SLO config YAML")
    parser.add_argument("--service", help="Evaluate a single service only")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate but do not page PagerDuty")
    args = parser.parse_args()

    for var in ("DD_API_KEY", "DD_APP_KEY"):
        if var not in os.environ:
            print(f"ERROR: {var} is not set.", file=sys.stderr)
            sys.exit(2)

    configs = load_configs(args.config, service_filter=args.service)
    if not configs:
        print(f"No services found in {args.config}" + (f" matching '{args.service}'" if args.service else ""), file=sys.stderr)
        sys.exit(2)

    dd = DatadogClient()
    pd_key = os.environ.get("PAGERDUTY_ROUTING_KEY", "")
    pd = PagerDutyClient(pd_key) if pd_key else None

    any_alert = False
    for cfg in configs:
        try:
            windows, alert = evaluate_burn_rates(dd, cfg)
        except requests.RequestException as exc:
            print(f"ERROR: Datadog query failed for {cfg.service_name}: {exc}", file=sys.stderr)
            continue

        print_service_report(cfg, windows, alert)

        if alert:
            any_alert = True
            if pd:
                pd.trigger(cfg.service_name, alert, dry_run=args.dry_run)
            elif not args.dry_run:
                print(f"  WARNING: PAGERDUTY_ROUTING_KEY not set — alert not sent")

    sys.exit(1 if any_alert else 0)


if __name__ == "__main__":
    main()
