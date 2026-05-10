#!/usr/bin/env python3
"""
Gate 4 — post-promotion SLO conformance check.

Runs 60 minutes after full production promotion. Validates:
  1. All owned SLOs at or above target for the observation window
  2. Monthly error budget consumed < 5% in the observation window
  3. Synthetic transaction pass rate > 99.9%
  4. Top 5 downstream consumer error rates have not increased

Exits 0 (all checks pass) or 1 (any check fails). JSON report written to
--output path if specified — consumed by the CI gate step.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
import yaml


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SLODefinition:
    id: str
    name: str
    target: float   # e.g. 0.999


@dataclass
class ServiceGateConfig:
    service: str
    slos: list[SLODefinition]
    downstream_consumers: list[str]
    synthetic_monitor_id: str


def load_config(config_path: str, service: str) -> ServiceGateConfig:
    with open(config_path) as fh:
        raw = yaml.safe_load(fh)
    svc = raw["services"][service]
    return ServiceGateConfig(
        service=service,
        slos=[SLODefinition(**s) for s in svc.get("slos", [])],
        downstream_consumers=svc.get("downstream_consumers", []),
        synthetic_monitor_id=svc.get("synthetic_monitor_id", ""),
    )


# ---------------------------------------------------------------------------
# Datadog clients
# ---------------------------------------------------------------------------

class DatadogClient:
    BASE = "https://api.datadoghq.com/api/v1"

    def __init__(self) -> None:
        self._headers = {
            "DD-API-KEY": os.environ["DD_API_KEY"],
            "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        }

    def slo_history(self, slo_id: str, from_ts: int, to_ts: int) -> dict:
        resp = requests.get(
            f"{self.BASE}/slo/{slo_id}/history",
            headers=self._headers,
            params={"from_ts": from_ts, "to_ts": to_ts},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def metric_avg(self, query: str, from_ts: int, to_ts: int) -> float:
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
        points = [p[1] for p in series[0]["pointlist"] if p[1] is not None]
        return sum(points) / len(points) if points else 0.0

    def synthetic_results(self, monitor_id: str, from_ts: int, to_ts: int) -> float:
        resp = requests.get(
            f"{self.BASE}/synthetics/tests/{monitor_id}/results",
            headers=self._headers,
            params={"from_ts": from_ts, "to_ts": to_ts},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return 1.0
        passed = sum(1 for r in results if r.get("result", {}).get("passed", False))
        return passed / len(results)


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------

@dataclass
class GateCheck:
    name: str
    passed: bool
    observed: float
    threshold: float
    unit: str = ""
    detail: str = ""

    def display(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        obs = f"{self.observed:.4f}{self.unit}"
        thr = f"{self.threshold}{self.unit}"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"  [{status}] {self.name}: {obs} (threshold: {thr}){suffix}"


@dataclass
class GateReport:
    service: str
    timestamp: str
    window_minutes: int
    checks: list[GateCheck]
    passed: bool

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Gate 4 evaluator
# ---------------------------------------------------------------------------

# Monthly error budget thresholds
_BUDGET_MAX_CONSUMPTION_PCT = 5.0
_SYNTHETIC_PASS_RATE_MIN = 0.999
_DOWNSTREAM_ERROR_RATE_MAX_DELTA = 0.001   # 0.1pp


class Gate4Evaluator:
    def __init__(self, dd: DatadogClient, verbose: bool = False) -> None:
        self.dd = dd
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  {msg}")

    def check_slo_conformance(
        self, slo: SLODefinition, from_ts: int, to_ts: int
    ) -> GateCheck:
        self._log(f"Querying SLO history: {slo.name} ({slo.id})")
        history = self.dd.slo_history(slo.id, from_ts, to_ts)
        overall = history.get("overall", {})
        sli_value = overall.get("sli_value", None)
        if sli_value is None:
            return GateCheck(
                name=f"slo:{slo.name}",
                passed=False,
                observed=0.0,
                threshold=slo.target,
                unit="",
                detail="No SLI data returned",
            )
        return GateCheck(
            name=f"slo:{slo.name}",
            passed=sli_value >= slo.target,
            observed=round(sli_value, 6),
            threshold=slo.target,
        )

    def check_error_budget(
        self, slo: SLODefinition, from_ts: int, to_ts: int
    ) -> GateCheck:
        history = self.dd.slo_history(slo.id, from_ts, to_ts)
        overall = history.get("overall", {})
        # Datadog reports error_budget as fraction of total budget remaining
        budget_remaining = overall.get("error_budget", {}).get("remaining", {}).get("value", 100.0)
        # Monthly budget = (1 - target) * 100%
        monthly_budget_pct = (1 - slo.target) * 100
        # Consumed in this window = what was burned as % of monthly
        budget_consumed = max(0.0, monthly_budget_pct - budget_remaining) if budget_remaining < monthly_budget_pct else 0.0
        return GateCheck(
            name=f"error_budget_consumption:{slo.name}",
            passed=budget_consumed <= _BUDGET_MAX_CONSUMPTION_PCT,
            observed=round(budget_consumed, 4),
            threshold=_BUDGET_MAX_CONSUMPTION_PCT,
            unit="%",
        )

    def check_synthetic_pass_rate(
        self, monitor_id: str, from_ts: int, to_ts: int
    ) -> GateCheck:
        self._log(f"Querying synthetic monitor: {monitor_id}")
        pass_rate = self.dd.synthetic_results(monitor_id, from_ts, to_ts)
        return GateCheck(
            name="synthetic_pass_rate",
            passed=pass_rate >= _SYNTHETIC_PASS_RATE_MIN,
            observed=round(pass_rate, 6),
            threshold=_SYNTHETIC_PASS_RATE_MIN,
        )

    def check_downstream_impact(
        self, service: str, consumers: list[str], from_ts: int, window_seconds: int
    ) -> list[GateCheck]:
        checks = []
        baseline_from = from_ts - window_seconds
        for consumer in consumers:
            tag = f"service:{consumer}"
            q = f"sum:trace.{consumer}.request.errors{{{tag}}}.as_rate() / sum:trace.{consumer}.request.hits{{{tag}}}.as_rate()"
            baseline_rate = self.dd.metric_avg(q, baseline_from, from_ts)
            current_rate = self.dd.metric_avg(q, from_ts, from_ts + window_seconds)
            delta = current_rate - baseline_rate
            checks.append(GateCheck(
                name=f"downstream_impact:{consumer}",
                passed=delta <= _DOWNSTREAM_ERROR_RATE_MAX_DELTA,
                observed=round(delta * 100, 4),
                threshold=_DOWNSTREAM_ERROR_RATE_MAX_DELTA * 100,
                unit="pp",
                detail=f"baseline={baseline_rate:.4f} current={current_rate:.4f}",
            ))
        return checks

    def evaluate(self, cfg: ServiceGateConfig, window_minutes: int) -> GateReport:
        to_ts = int(time.time())
        window_seconds = window_minutes * 60
        from_ts = to_ts - window_seconds

        checks: list[GateCheck] = []

        for slo in cfg.slos:
            checks.append(self.check_slo_conformance(slo, from_ts, to_ts))
            checks.append(self.check_error_budget(slo, from_ts, to_ts))

        if cfg.synthetic_monitor_id:
            checks.append(self.check_synthetic_pass_rate(cfg.synthetic_monitor_id, from_ts, to_ts))

        if cfg.downstream_consumers:
            checks.extend(
                self.check_downstream_impact(cfg.service, cfg.downstream_consumers, from_ts, window_seconds)
            )

        return GateReport(
            service=cfg.service,
            timestamp=datetime.now(timezone.utc).isoformat(),
            window_minutes=window_minutes,
            checks=checks,
            passed=all(c.passed for c in checks),
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gate 4 — post-promotion SLO conformance check")
    parser.add_argument("--service", required=True, help="Service name (must match config key)")
    parser.add_argument("--window-minutes", type=int, default=60, help="Observation window in minutes")
    parser.add_argument("--config", default="config/gate-thresholds.yaml", help="Path to gate config YAML")
    parser.add_argument("--output", metavar="PATH", help="Write JSON report to this path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    for var in ("DD_API_KEY", "DD_APP_KEY"):
        if var not in os.environ:
            print(f"ERROR: {var} is not set.", file=sys.stderr)
            sys.exit(2)

    cfg = load_config(args.config, args.service)
    dd = DatadogClient()
    evaluator = Gate4Evaluator(dd, verbose=args.verbose)

    print(f"Running Gate 4 for {args.service} (window: {args.window_minutes}m)...")
    try:
        report = evaluator.evaluate(cfg, args.window_minutes)
    except requests.RequestException as exc:
        print(f"ERROR: Datadog request failed: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"\nGate 4 {'PASSED' if report.passed else 'FAILED'} — {report.service}")
    for check in report.checks:
        print(check.display())

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"\nReport written to {args.output}")

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
