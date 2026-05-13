#!/usr/bin/env python3
"""
Chaos experiment runner — measures baseline, injects fault, monitors abort
conditions, then generates a structured report.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml


# ---------------------------------------------------------------------------
# Experiment definition
# ---------------------------------------------------------------------------

@dataclass
class AbortConditions:
    order_error_rate_max: float = 0.02
    payment_success_min: float = 0.98
    error_budget_consumed_max_pct: float = 10.0


@dataclass
class ExperimentConfig:
    name: str
    target: str
    fault_type: str          # pod_kill | node_drain | network_latency | cpu_stress | rds_failover
    fault_duration_seconds: int
    observation_window_seconds: int
    services: list[str]
    hypothesis: str
    abort_conditions: AbortConditions = field(default_factory=AbortConditions)
    namespace: str = "production"
    fault_params: dict = field(default_factory=dict)


def load_experiment(path: str) -> ExperimentConfig:
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    ac_raw = raw.pop("abort_conditions", {})
    return ExperimentConfig(
        **{k: v for k, v in raw.items() if k != "abort_conditions"},
        abort_conditions=AbortConditions(**ac_raw) if ac_raw else AbortConditions(),
    )


# ---------------------------------------------------------------------------
# Datadog metrics collector
# ---------------------------------------------------------------------------

@dataclass
class MetricsSnapshot:
    timestamp: str
    error_rate: float
    latency_p99_ms: float
    payment_success_rate: float
    throughput_rps: float


class DatadogMetricsCollector:
    BASE = "https://api.datadoghq.com/api/v1"

    def __init__(self) -> None:
        self._headers = {
            "DD-API-KEY": os.environ["DD_API_KEY"],
            "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        }

    def _query_avg(self, q: str, from_ts: int, to_ts: int) -> float:
        resp = requests.get(
            f"{self.BASE}/query",
            headers=self._headers,
            params={"query": q, "from": from_ts, "to": to_ts},
            timeout=15,
        )
        resp.raise_for_status()
        series = resp.json().get("series", [])
        if not series or not series[0].get("pointlist"):
            return 0.0
        points = [p[1] for p in series[0]["pointlist"] if p[1] is not None]
        return sum(points) / len(points) if points else 0.0

    def snapshot(self, services: list[str], window_seconds: int) -> MetricsSnapshot:
        to_ts = int(time.time())
        from_ts = to_ts - window_seconds
        tag_filter = ",".join(f"service:{s}" for s in services)

        error_rate = self._query_avg(
            f"sum:trace.request.errors{{{tag_filter}}}.as_rate() / "
            f"sum:trace.request.hits{{{tag_filter}}}.as_rate()",
            from_ts, to_ts,
        )
        latency = self._query_avg(
            f"p99:trace.request.duration{{{tag_filter}}}",
            from_ts, to_ts,
        )
        payment_success = self._query_avg(
            "avg:payment.transaction.success_rate{*}",
            from_ts, to_ts,
        )
        throughput = self._query_avg(
            f"sum:trace.request.hits{{{tag_filter}}}.as_rate()",
            from_ts, to_ts,
        )
        return MetricsSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            error_rate=round(error_rate, 6),
            latency_p99_ms=round(latency, 1),
            payment_success_rate=round(payment_success, 6),
            throughput_rps=round(throughput, 2),
        )


# ---------------------------------------------------------------------------
# Fault injector
# ---------------------------------------------------------------------------

class FaultInjector:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def _run(self, cmd: list[str]) -> None:
        if self.dry_run:
            print(f"  [dry-run] {' '.join(cmd)}")
            return
        subprocess.run(cmd, check=True)

    def inject(self, cfg: ExperimentConfig) -> None:
        print(f"  Injecting fault: {cfg.fault_type} → {cfg.target}")
        if cfg.fault_type == "pod_kill":
            self._run(["kubectl", "delete", "pod", cfg.target, "-n", cfg.namespace, "--grace-period=0"])
        elif cfg.fault_type == "node_drain":
            self._run(["kubectl", "drain", cfg.target, "--ignore-daemonsets", "--delete-emptydir-data", "--force"])
        elif cfg.fault_type == "network_latency":
            delay_ms = cfg.fault_params.get("delay_ms", 200)
            self._run(["kubectl", "exec", "-n", cfg.namespace, cfg.target, "--",
                       "tc", "qdisc", "add", "dev", "eth0", "root", "netem", "delay", f"{delay_ms}ms"])
        elif cfg.fault_type == "cpu_stress":
            workers = cfg.fault_params.get("workers", 4)
            self._run(["kubectl", "exec", "-n", cfg.namespace, cfg.target, "--",
                       "stress-ng", "--cpu", str(workers), "--timeout", str(cfg.fault_duration_seconds)])
        elif cfg.fault_type == "rds_failover":
            db_id = cfg.fault_params.get("db_cluster_id", cfg.target)
            self._run(["aws", "rds", "failover-db-cluster", "--db-cluster-identifier", db_id])
        else:
            raise ValueError(f"Unknown fault_type: {cfg.fault_type}")

    def restore(self, cfg: ExperimentConfig) -> None:
        print(f"  Restoring: {cfg.fault_type} → {cfg.target}")
        if cfg.fault_type == "node_drain":
            self._run(["kubectl", "uncordon", cfg.target])
        elif cfg.fault_type == "network_latency":
            self._run(["kubectl", "exec", "-n", cfg.namespace, cfg.target, "--",
                       "tc", "qdisc", "del", "dev", "eth0", "root"])
        # pod_kill and rds_failover self-heal; cpu_stress has built-in timeout


# ---------------------------------------------------------------------------
# Abort monitor
# ---------------------------------------------------------------------------

class AbortMonitor:
    def __init__(self, collector: DatadogMetricsCollector, conditions: AbortConditions) -> None:
        self.collector = collector
        self.conditions = conditions

    def check(self, services: list[str]) -> tuple[bool, str]:
        snap = self.collector.snapshot(services, window_seconds=60)
        if snap.error_rate > self.conditions.order_error_rate_max:
            return True, f"error_rate {snap.error_rate:.4f} > {self.conditions.order_error_rate_max}"
        if snap.payment_success_rate < self.conditions.payment_success_min:
            return True, f"payment_success_rate {snap.payment_success_rate:.4f} < {self.conditions.payment_success_min}"
        return False, ""


# ---------------------------------------------------------------------------
# Experiment report
# ---------------------------------------------------------------------------

@dataclass
class ExperimentReport:
    experiment_name: str
    hypothesis: str
    started_at: str
    fault_injected_at: Optional[str]
    fault_removed_at: Optional[str]
    completed_at: str
    aborted: bool
    abort_reason: str
    baseline: Optional[MetricsSnapshot]
    during_fault: list[MetricsSnapshot]
    recovery: Optional[MetricsSnapshot]
    recovery_time_seconds: Optional[float]
    findings: str

    def to_dict(self) -> dict:
        return asdict(self)

    def print_summary(self) -> None:
        print(f"\n{'─' * 70}")
        print(f"Experiment: {self.experiment_name}")
        print(f"Hypothesis: {self.hypothesis}")
        print(f"Aborted:    {'YES — ' + self.abort_reason if self.aborted else 'No'}")
        if self.baseline:
            print(f"\nBaseline:")
            print(f"  error_rate={self.baseline.error_rate}  latency_p99={self.baseline.latency_p99_ms}ms  payment={self.baseline.payment_success_rate}")
        if self.recovery:
            print(f"\nPost-recovery:")
            print(f"  error_rate={self.recovery.error_rate}  latency_p99={self.recovery.latency_p99_ms}ms  payment={self.recovery.payment_success_rate}")
        if self.recovery_time_seconds is not None:
            print(f"\nRecovery time: {self.recovery_time_seconds:.1f}s")
        print(f"\nFindings: {self.findings}")
        print(f"{'─' * 70}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class ChaosRunner:
    _POLL_INTERVAL = 15   # seconds between abort condition checks

    def __init__(
        self,
        collector: DatadogMetricsCollector,
        injector: FaultInjector,
        monitor: AbortMonitor,
    ) -> None:
        self.collector = collector
        self.injector = injector
        self.monitor = monitor

    def run(self, cfg: ExperimentConfig) -> ExperimentReport:
        started_at = datetime.now(timezone.utc).isoformat()
        print(f"\n[{started_at}] Starting experiment: {cfg.name}")
        print(f"Hypothesis: {cfg.hypothesis}\n")

        print("Phase 1: Measuring 10-minute baseline...")
        time.sleep(60) if not self.injector.dry_run else None
        baseline = self.collector.snapshot(cfg.services, window_seconds=600)
        print(f"  Baseline captured: error_rate={baseline.error_rate}  p99={baseline.latency_p99_ms}ms")

        fault_injected_at = datetime.now(timezone.utc).isoformat()
        print(f"\nPhase 2: Injecting fault ({cfg.fault_type})...")
        self.injector.inject(cfg)

        during_fault: list[MetricsSnapshot] = []
        aborted = False
        abort_reason = ""
        fault_start = time.time()

        print(f"\nPhase 3: Monitoring ({cfg.observation_window_seconds}s, abort checks every {self._POLL_INTERVAL}s)...")
        while time.time() - fault_start < cfg.fault_duration_seconds:
            time.sleep(self._POLL_INTERVAL) if not self.injector.dry_run else None
            snap = self.collector.snapshot(cfg.services, window_seconds=60)
            during_fault.append(snap)
            print(f"  [{snap.timestamp}] error_rate={snap.error_rate:.4f}  p99={snap.latency_p99_ms}ms  payment={snap.payment_success_rate:.4f}")

            should_abort, reason = self.monitor.check(cfg.services)
            if should_abort:
                aborted = True
                abort_reason = reason
                print(f"\n  ABORT CONDITION MET: {reason}")
                break

        fault_removed_at = datetime.now(timezone.utc).isoformat()
        print(f"\nPhase 4: Removing fault...")
        self.injector.restore(cfg)

        print("Phase 5: Measuring recovery (5-min window)...")
        time.sleep(300) if not self.injector.dry_run else None
        recovery_snap = self.collector.snapshot(cfg.services, window_seconds=300)

        recovery_time: Optional[float] = None
        recovery_start = time.time()
        if not aborted:
            # Poll until error rate returns to within 110% of baseline
            while time.time() - recovery_start < 600:
                current = self.collector.snapshot(cfg.services, window_seconds=60)
                if current.error_rate <= baseline.error_rate * 1.1:
                    recovery_time = time.time() - recovery_start
                    break
                time.sleep(self._POLL_INTERVAL) if not self.injector.dry_run else None

        findings = (
            f"Experiment {'aborted' if aborted else 'completed'}. "
            f"{'Abort: ' + abort_reason + '. ' if aborted else ''}"
            f"Recovery time: {recovery_time:.0f}s." if recovery_time else "Recovery not measured (aborted or timed out)."
        )

        return ExperimentReport(
            experiment_name=cfg.name,
            hypothesis=cfg.hypothesis,
            started_at=started_at,
            fault_injected_at=fault_injected_at,
            fault_removed_at=fault_removed_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            aborted=aborted,
            abort_reason=abort_reason,
            baseline=baseline,
            during_fault=during_fault,
            recovery=recovery_snap,
            recovery_time_seconds=recovery_time,
            findings=findings,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Chaos experiment runner")
    parser.add_argument("--experiment", required=True, help="Path to experiment YAML definition")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without injecting faults")
    parser.add_argument("--report-dir", default="reports", help="Directory for JSON experiment reports")
    args = parser.parse_args()

    for var in ("DD_API_KEY", "DD_APP_KEY"):
        if var not in os.environ and not args.dry_run:
            print(f"ERROR: {var} is not set.", file=sys.stderr)
            sys.exit(2)

    cfg = load_experiment(args.experiment)
    dd = DatadogMetricsCollector()
    injector = FaultInjector(dry_run=args.dry_run)
    monitor = AbortMonitor(dd, cfg.abort_conditions)
    runner = ChaosRunner(dd, injector, monitor)

    report = runner.run(cfg)
    report.print_summary()

    Path(args.report_dir).mkdir(parents=True, exist_ok=True)
    report_path = Path(args.report_dir) / f"{cfg.name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    with open(report_path, "w") as fh:
        json.dump(report.to_dict(), fh, indent=2, default=str)
    print(f"\nReport saved: {report_path}")

    sys.exit(1 if report.aborted else 0)


if __name__ == "__main__":
    main()
