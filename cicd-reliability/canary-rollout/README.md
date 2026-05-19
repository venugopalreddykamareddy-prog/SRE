# Canary Rollout

A progressive delivery framework that releases new versions to a small percentage of production traffic before full promotion, with automated rollback triggered by real-time SLO degradation signals. This is the mechanism that makes deploying 15–20 times per week possible without each deployment being a reliability gamble.

---

## Background

At Starbucks' deployment velocity — the digital platform team was pushing multiple releases daily across dozens of microservices — traditional blue/green deployments created two problems:

1. **Binary risk**: A broken release either hit 0% or 100% of users. There was no middle ground between "not deployed" and "fully deployed."
2. **Slow feedback loops**: Staging environments, no matter how well maintained, don't perfectly replicate production traffic patterns. Real issues often only appeared at production scale.

Canary deployments solved both. By routing 5% of production traffic to the new version, we get real-world signal on reliability and business metrics before committing to full promotion — while limiting customer exposure to any regression to a small fraction of traffic.

---

## Canary Strategy

### Traffic Split Stages

```
Stage 0: Canary Deploy
  └── 5% production traffic → new version
  └── 95% production traffic → stable version
  └── Observation: 30 minutes
  └── Gate check → pass/fail

Stage 1: Expanded Canary
  └── 25% production traffic → new version
  └── 75% production traffic → stable version
  └── Observation: 30 minutes
  └── Gate check → pass/fail

Stage 2: Full Promotion
  └── 100% production traffic → new version
  └── Observation: 60 minutes
  └── Final gate check (Gate 4)

Rollback (any stage):
  └── Traffic immediately returned to stable version
  └── New version pods scaled to 0
  └── PagerDuty incident created
```

Traffic splitting is implemented via Istio `VirtualService` weight rules on the service mesh. No changes to Kubernetes Services or DNS are required for traffic routing.

---

## Automatic Rollback Triggers

Rollback is triggered automatically — without human intervention — when any of the following conditions are met during the canary observation window:

| Signal | Threshold | Source |
|---|---|---|
| Error rate delta above stable | > 0.1 pp | Datadog SLO monitor |
| p99 latency delta above stable | > 50ms | Datadog APM |
| Order completion rate drop | > 0.5% | Business metric monitor |
| Payment success rate drop | > 0.2% | Business metric monitor |
| SLO burn rate multiplier | > 2× baseline | Error budget monitor |
| AI anomaly detector score | > 0.75 | Log anomaly pipeline |
| Pod restart rate | > 2 restarts/min | Kubernetes events |

Rollback decisions are made by the canary controller, not by a human reading a dashboard. During a critical morning rush deployment window (7–9 AM PST), there is no time for a human to evaluate metrics and make a call — the system must act within seconds.

---

## Repository Structure

```
canary-rollout/
├── istio/
│   ├── virtual-service-template.yaml     # VirtualService template for traffic splitting
│   ├── destination-rule.yaml             # DestinationRule for subset routing
│   └── canary-progressions/
│       ├── 5pct-canary.yaml              # 5% canary VirtualService
│       ├── 25pct-canary.yaml             # 25% expanded canary
│       └── 100pct-promotion.yaml         # Full promotion
├── argocd/
│   ├── rollout-definition.yaml          # Argo Rollouts CRD for canary strategy
│   └── analysis-template.yaml          # AnalysisTemplate for metric-based gate
├── controllers/
│   ├── canary_controller.py             # Canary gate evaluation and promotion logic (Python)
│   └── rollback_handler.go              # Rollback webhook receiver — scales canary to 0, pages PagerDuty (Go)
├── monitoring/
│   ├── canary-dashboard.json           # Grafana: canary vs. stable side-by-side
│   └── canary-alerts.yaml             # Datadog monitors for canary-specific signals
├── scripts/
│   ├── promote.sh                       # Manual promotion command (with confirmation)
│   ├── rollback.sh                      # Manual rollback command
│   └── canary-status.sh                # Current canary state and metrics summary
└── config/
    └── rollout-config.yaml             # Thresholds, timing, service-specific overrides
```

---

## Controller Implementations

### `canary_controller.py` — Gate evaluator (Python)

Polls Datadog for canary vs. stable fleet metrics over the observation window, evaluates each gate threshold from `config/rollout-config.yaml`, and calls the Argo Rollouts CLI to promote or abort. Runs as a Kubernetes Job triggered at the end of each observation window.

```bash
python controllers/canary_controller.py --service ordering-api --stage 1
python controllers/canary_controller.py --service ordering-api --stage 1 --dry-run
```

### `rollback_handler.go` — Rollback webhook (Go)

An HTTP server that receives rollback signals from Argo Rollouts analysis failures or direct CI triggers. On receipt it:

1. Finds all Deployments with `version=canary` for the service
2. Removes the canary label so Istio stops routing traffic to them
3. Scales canary Deployments to 0 replicas and confirms scale-down
4. Fires a PagerDuty P2 incident with the failing metric and observed value

Written in Go rather than Python because it runs as a **persistent service** (not a one-shot Job) and needs to handle concurrent rollback requests without a process-per-request model. The Kubernetes and HTTP client libraries in Go's standard ecosystem (`client-go`, `net/http`) are also a better fit for a long-running webhook receiver than Python's threading model.

```bash
# Build
go build -o rollback_handler ./controllers/rollback_handler.go

# Run
PAGERDUTY_ROUTING_KEY=xxx ./rollback_handler --port 8080 --namespace production

# Trigger a rollback manually
curl -X POST http://localhost:8080/rollback \
  -H 'Content-Type: application/json' \
  -d '{"service":"ordering-api","version":"v2.14.3","failing_metric":"latency_p99_delta","observed_value":78.2,"threshold":50}'
```

---

## Argo Rollouts Integration

The canary progression is managed by [Argo Rollouts](https://argoproj.github.io/rollouts/), which provides the CRD-based rollout controller on top of Kubernetes. The `AnalysisTemplate` evaluates Datadog metrics to determine pass/fail:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: ordering-api-canary-analysis
spec:
  metrics:
  - name: error-rate-delta
    interval: 60s
    successCondition: result < 0.001   # < 0.1 pp delta
    failureLimit: 2
    provider:
      datadog:
        query: |
          (sum:trace.ordering-api.request.errors{version:canary}.as_rate()
           / sum:trace.ordering-api.request.hits{version:canary}.as_rate())
          -
          (sum:trace.ordering-api.request.errors{version:stable}.as_rate()
           / sum:trace.ordering-api.request.hits{version:stable}.as_rate())

  - name: p99-latency-delta
    interval: 60s
    successCondition: result < 50      # < 50ms above stable p99
    failureLimit: 2
    provider:
      datadog:
        query: |
          p99:trace.ordering-api.request.duration{version:canary}
          - p99:trace.ordering-api.request.duration{version:stable}
```

`failureLimit: 2` means the metric must exceed the threshold in 2 consecutive evaluation intervals before rollback fires — preventing single noisy data points from triggering unnecessary rollbacks.

---

## Service-Specific Configuration

Not all services have the same risk profile. High-stakes services (ordering, payment) move through canary stages more slowly. Low-risk internal services move faster.

```yaml
# config/rollout-config.yaml
services:
  ordering-api:
    canary_pct: 5
    observation_minutes: 30
    expanded_canary_pct: 25
    expanded_observation_minutes: 30
    auto_promote: false              # Requires human approval for full promotion

  menu-service:
    canary_pct: 10
    observation_minutes: 15
    expanded_canary_pct: 50
    expanded_observation_minutes: 15
    auto_promote: true               # Automatic if gate passes

  internal-analytics:
    canary_pct: 20
    observation_minutes: 10
    auto_promote: true
    skip_expanded_canary: true
```

---

## Canary Observability Dashboard

The canary dashboard provides a side-by-side view of canary vs. stable fleet metrics:

- Error rate (canary vs. stable, with delta)
- p50 / p95 / p99 latency (canary vs. stable)
- Throughput (requests/sec to each version)
- Order completion rate (canary vs. stable)
- Active pods per version
- Current gate status and time remaining in observation window

The dashboard is linked directly from every canary deployment Slack notification, so engineers can monitor progress without manually constructing queries.

---

## Deployment Notification Flow

When a canary deployment starts:

```
#deploys channel:
[CANARY START] ordering-api v2.14.3 → 5% of production traffic
By: @engineer | PR: #1847 | Observation window: 30 min
Canary dashboard: <link>
Rollout status: <link>
```

When a gate check completes:
```
[CANARY GATE PASS] ordering-api v2.14.3 — Gate 3 passed
Error rate delta: +0.02pp (threshold: 0.1pp) ✓
p99 latency delta: +12ms (threshold: 50ms) ✓
Order completion: stable ✓
→ Promoting to 25%...
```

When rollback fires:
```
[CANARY ROLLBACK] ordering-api v2.14.3 — AUTOMATIC ROLLBACK TRIGGERED
Failing metric: p99 latency delta +78ms (threshold: 50ms)
Traffic returned to stable fleet. Canary pods scaled to 0.
PagerDuty: PD-XXXXX | Runbook: <link>
```

---

## Lessons Learned

**Business metrics at the canary stage are more valuable than technical metrics.** A canary with a healthy error rate can still degrade order completion if it introduces a UI regression or a subtle cart-calculation bug. Business metrics catch what APM metrics miss.

**Automatic rollback requires high confidence in your baseline.** During periods of unusual traffic (holiday campaigns, new store openings), the baseline itself is noisy. We added a baseline variance check: if the stable fleet's own metrics are fluctuating heavily, canary gate thresholds automatically widen to avoid false rollbacks.

**`auto_promote: false` for revenue-critical services pays for itself.** The extra 2-minute human review before full promotion of the ordering API has caught 3 cases where a gate technically passed but had a suspicious pattern that a human noticed. It's worth it.

---

## Prerequisites

- Kubernetes 1.25+ with Argo Rollouts
- Istio service mesh (for traffic splitting)
- Datadog with APM and custom metrics
- GitHub Actions or equivalent CI/CD runner
- ArgoCD (for GitOps state management)
