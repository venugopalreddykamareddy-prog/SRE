#!/usr/bin/env python3
"""
Canary gate controller — evaluates promotion gates and triggers rollback
when any metric exceeds configured thresholds.
"""

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
import yaml


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class GateThresholds:
    error_rate_delta_max_pp: float       # percentage points above stable
    latency_p99_delta_max_ms: float
    slo_burn_rate_multiplier_max: float
    anomaly_score_max: float
    pod_restart_rate_max: float          # restarts/min


@dataclass
class ServiceRolloutConfig:
    canary_pct: int
    observation_minutes: int
    expanded_canary_pct: int
    expanded_observation_minutes: int
    auto_promote: bool
    skip_expanded_canary: bool = False
    gates: GateThresholds = field(default_factory=lambda: GateThresholds(
        error_rate_delta_max_pp=0.1,
        latency_p99_delta_max_ms=50,
        slo_burn_rate_multiplier_max=2.0,
        anomaly_score_max=0.6,
        pod_restart_rate_max=2.0,
    ))


def load_rollout_config(config_path: str, service: str) -> ServiceRolloutConfig:
    with open(config_path) as fh:
        raw = yaml.safe_load(fh)
    svc = raw["services"][service]
    gates_raw = raw.get("gates", {})
    return ServiceRolloutConfig(
        canary_pct=svc.get("canary_pct", 5),
        observation_minutes=svc.get("observation_minutes", 30),
        expanded_canary_pct=svc.get("expanded_canary_pct", 25),
        expanded_observation_minutes=svc.get("expanded_observation_minutes", 30),
        auto_promote=svc.get("auto_promote", False),
        skip_expanded_canary=svc.get("skip_expanded_canary", False),
        gates=GateThresholds(
            error_rate_delta_max_pp=gates_raw.get("error_rate_delta_max_pp", 0.1),
            latency_p99_delta_max_ms=gates_raw.get("latency_p99_delta_max_ms", 50),
            slo_burn_rate_multiplier_max=gates_raw.get("slo_burn_rate_multiplier_max", 2.0),
            anomaly_score_max=gates_raw.get("anomaly_score_max", 0.6),
            pod_restart_rate_max=gates_raw.get("pod_restart_rate_max", 2.0),
        ),
    )


# ---------------------------------------------------------------------------
# Datadog client
# ---------------------------------------------------------------------------

@dataclass
class MetricWindow:
    error_rate: float
    latency_p99_ms: float
    slo_burn_rate: float
    pod_restart_rate: float


class DatadogMetricClient:
    BASE = "https://api.datadoghq.com/api/v1"

    def __init__(self) -> None:
        self._headers = {
            "DD-API-KEY": os.environ["DD_API_KEY"],
            "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        }

    def _query(self, q: str, window_seconds: int) -> float:
        now = int(time.time())
        resp = requests.get(
            f"{self.BASE}/query",
            headers=self._headers,
            params={"query": q, "from": now - window_seconds, "to": now},
            timeout=15,
        )
        resp.raise_for_status()
        series = resp.json().get("series", [])
        if not series or not series[0].get("pointlist"):
            return 0.0
        points = [p[1] for p in series[0]["pointlist"] if p[1] is not None]
        return sum(points) / len(points) if points else 0.0

    def snapshot(self, service: str, version: str, window_seconds: int) -> MetricWindow:
        tag = f"service:{service},version:{version}"
        error_rate = self._query(
            f"sum:trace.{service}.request.errors{{{tag}}}.as_rate() / "
            f"sum:trace.{service}.request.hits{{{tag}}}.as_rate()",
            window_seconds,
        )
        latency = self._query(
            f"p99:trace.{service}.request.duration{{{tag}}}",
            window_seconds,
        )
        burn = self._query(
            f"avg:datadog.estimated_usage.slo_budget_used{{{tag}}}",
            window_seconds,
        )
        restarts = self._query(
            f"sum:kubernetes.containers.restarts{{{tag}}}.as_rate()",
            window_seconds,
        )
        return MetricWindow(
            error_rate=error_rate,
            latency_p99_ms=latency,
            slo_burn_rate=burn,
            pod_restart_rate=restarts * 60,  # convert to /min
        )


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    passed: bool
    observed: float
    threshold: float
    unit: str = ""

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"  [{status}] {self.name}: {self.observed:.4f}{self.unit} (threshold: {self.threshold}{self.unit})"


@dataclass
class GateResult:
    passed: bool
    checks: list[CheckResult]
    canary: MetricWindow
    stable: MetricWindow

    def summary(self) -> str:
        lines = [f"Gate {'PASSED' if self.passed else 'FAILED'}"]
        lines.extend(str(c) for c in self.checks)
        return "\n".join(lines)


class CanaryGateEvaluator:
    def __init__(self, dd: DatadogMetricClient, thresholds: GateThresholds) -> None:
        self.dd = dd
        self.t = thresholds

    def evaluate(self, service: str, lookback_seconds: int) -> GateResult:
        canary = self.dd.snapshot(service, "canary", lookback_seconds)
        stable = self.dd.snapshot(service, "stable", lookback_seconds)

        err_delta = canary.error_rate - stable.error_rate
        lat_delta = canary.latency_p99_ms - stable.latency_p99_ms
        burn_ratio = (canary.slo_burn_rate / stable.slo_burn_rate) if stable.slo_burn_rate > 0 else 0.0

        checks = [
            CheckResult(
                "error_rate_delta",
                err_delta <= self.t.error_rate_delta_max_pp,
                round(err_delta * 100, 4),
                self.t.error_rate_delta_max_pp * 100,
                "pp",
            ),
            CheckResult(
                "latency_p99_delta",
                lat_delta <= self.t.latency_p99_delta_max_ms,
                round(lat_delta, 1),
                self.t.latency_p99_delta_max_ms,
                "ms",
            ),
            CheckResult(
                "slo_burn_rate_ratio",
                burn_ratio <= self.t.slo_burn_rate_multiplier_max,
                round(burn_ratio, 2),
                self.t.slo_burn_rate_multiplier_max,
                "x",
            ),
            CheckResult(
                "pod_restart_rate",
                canary.pod_restart_rate <= self.t.pod_restart_rate_max,
                round(canary.pod_restart_rate, 2),
                self.t.pod_restart_rate_max,
                "/min",
            ),
        ]

        # TODO: anomaly_score check is missing — need to wire in the ML anomaly detector output
        return GateResult(
            passed=all(c.passed for c in checks),
            checks=checks,
            canary=canary,
            stable=stable,
        )


# ---------------------------------------------------------------------------
# Slack notifier
# ---------------------------------------------------------------------------

class SlackNotifier:
    def __init__(self) -> None:
        self._webhook = os.environ.get("SLACK_WEBHOOK_URL", "")

    def post(self, service: str, version: str, stage: int, result: GateResult) -> None:
        if not self._webhook:
            return
        status = "GATE PASS" if result.passed else "GATE FAIL"
        emoji = ":white_check_mark:" if result.passed else ":rotating_light:"
        checks_text = "\n".join(str(c) for c in result.checks)
        payload = {
            "text": (
                f"{emoji} *[CANARY {status}]* `{service}` v`{version}` — Stage {stage}\n"
                f"```{checks_text}```"
            )
        }
        try:
            requests.post(self._webhook, json=payload, timeout=10).raise_for_status()
        except requests.RequestException:
            pass  # non-fatal; gate result is authoritative


# ---------------------------------------------------------------------------
# Argo Rollouts integration
# ---------------------------------------------------------------------------

class ArgoRolloutClient:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def _kubectl(self, *args: str) -> None:
        cmd = ["kubectl", "argo", "rollouts", *args]
        if self.dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
            return
        subprocess.run(cmd, check=True)

    def promote(self, service: str, namespace: str = "production") -> None:
        self._kubectl("promote", service, "-n", namespace)

    def abort(self, service: str, namespace: str = "production") -> None:
        self._kubectl("abort", service, "-n", namespace)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(service: str, stage: int, config_path: str, dry_run: bool) -> int:
    config = load_rollout_config(config_path, service)
    lookback = config.observation_minutes * 60

    dd = DatadogMetricClient()
    evaluator = CanaryGateEvaluator(dd, config.gates)
    notifier = SlackNotifier()
    argo = ArgoRolloutClient(dry_run=dry_run)

    version = os.environ.get("DEPLOY_VERSION", "unknown")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] Evaluating canary gate — service={service} stage={stage} lookback={lookback}s")

    try:
        result = evaluator.evaluate(service, lookback)
    except requests.RequestException as exc:
        print(f"ERROR: Datadog query failed: {exc}", file=sys.stderr)
        return 2

    print(result.summary())
    notifier.post(service, version, stage, result)

    if result.passed:
        if config.auto_promote or stage == 1:
            print(f"Promoting {service} to next stage...")
            argo.promote(service)
        else:
            # TODO: post a Slack message with an approval link instead of just printing
            print(f"Gate passed. Awaiting manual approval before full promotion (auto_promote=false).")
        return 0
    else:
        print(f"Gate failed. Initiating rollback for {service}...")
        argo.abort(service)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Canary gate evaluator")
    parser.add_argument("--service", required=True, help="Service name (must match rollout config key)")
    parser.add_argument("--stage", type=int, choices=[1, 2], default=1, help="Canary stage (1=5%%, 2=25%%)")
    parser.add_argument("--config", default="config/rollout-config.yaml", help="Path to rollout-config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate gate but skip ArgoCD commands")
    args = parser.parse_args()

    sys.exit(run(args.service, args.stage, args.config, args.dry_run))


if __name__ == "__main__":
    main()
