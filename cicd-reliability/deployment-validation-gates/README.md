# Deployment Validation Gates

A multi-stage deployment validation framework that enforces automated quality checks at every phase of the release pipeline — from code merge to full production promotion. This system reduced production deployment failures by **85%** across the digital ordering platform by catching regressions before they reached customers.

---

## Background

Before this framework, the deployment process relied primarily on unit tests and a manual QA sign-off before production push. Deployment-related incidents accounted for 62% of all P1 and P2 incidents over a 6-month baseline period. The failure patterns were consistent:

- **Configuration drift** — a value correct in staging wasn't updated in production secrets
- **Dependency incompatibility** — a service deployed fine in isolation but broke a downstream consumer
- **Resource misconfiguration** — new service instances with incorrect memory limits causing OOMKill at load
- **SLO regression** — a functionally passing feature that introduced latency regression, visible only under production traffic

The deployment validation gates framework addresses all four patterns with automated checks that block promotion rather than relying on human review to catch what machines can detect automatically.

---

## Gate Architecture

Deployments progress through four sequential stages. Each stage has a pass/fail gate; a gate failure blocks promotion to the next stage and triggers an automatic rollback of any partial changes.

```
Code Merge
    │
    ▼ ══════════════════════════
    │  GATE 1: Pre-Deployment   │ ← runs on merge to main
    │  Static validation        │
    ══════════════════════════ ▼
    │  GATE 2: Staging          │ ← runs during staging deploy
    │  Integration + load tests │
    ══════════════════════════ ▼
    │  GATE 3: Canary (5%)      │ ← runs 30 min after canary release
    │  Live traffic validation  │
    ══════════════════════════ ▼
    │  GATE 4: Post-Promotion   │ ← runs 60 min after full promotion
    │  SLO conformance check    │
    ══════════════════════════
    │
    ▼
Full Production
```

---

## Gate 1 — Pre-Deployment Validation

Runs as a GitHub Actions workflow on every merge to `main`. Blocks promotion to staging.

**Checks:**
- **Container image scan** — Trivy CVE scan; blocks on CRITICAL severity findings
- **Kubernetes manifest validation** — `kube-score` and `kubeval` against target cluster version
- **Secret reference validation** — confirms all `secretKeyRef` references exist in the target namespace
- **Resource limits enforcement** — all containers must declare CPU/memory requests and limits
- **Dependency version compatibility** — checks service's declared dependency versions against a compatibility matrix
- **SLO definition presence** — if a service owns an SLO, a corresponding monitor definition must exist

**Duration:** ~4 minutes

**On failure:** PR author notified via Slack; merge blocked until resolved.

---

## Gate 2 — Staging Integration Validation

Runs during deployment to the staging environment. Blocks promotion to canary.

**Checks:**
- **Smoke tests** — 50 synthetic transactions covering all critical user journeys (place order, redeem reward, view menu, payment confirmation)
- **Contract tests** — Pact consumer-driven contract tests between this service and its downstream consumers
- **Load test** — 5-minute load test at 150% of peak production TPS; p99 latency must remain within SLO bounds
- **Database migration dry-run** — if migrations are included, run against a staging replica and validate completion without errors
- **Health check stabilization** — all new pods must remain `Ready` for 5 consecutive minutes before gate passes

**Duration:** ~18 minutes

**On failure:** Deploy halted; staging environment rolled back; Jira ticket auto-created with test output.

---

## Gate 3 — Canary Traffic Validation

Runs 30 minutes after 5% canary release to production. Blocks full production promotion.

**Checks:**
- **Error rate delta** — canary error rate must not exceed baseline by more than 0.1 percentage points
- **Latency delta** — canary p99 latency must not exceed baseline p99 by more than 50ms
- **Business metric checks** — order completion rate and payment success rate must not degrade vs. baseline
- **SLO burn rate** — canary must not be consuming error budget at a rate faster than 2× baseline
- **Log anomaly check** — AI anomaly detector score for the canary deployment must remain below 0.6

All comparisons are against a 30-minute rolling baseline from the stable production fleet (the 95% non-canary traffic).

**Duration:** 30 minutes of observation + ~2 minutes of gate evaluation

**On failure:** Canary traffic immediately redirected to stable fleet; canary pods scaled to 0; PagerDuty P2 created.

---

## Gate 4 — Post-Promotion SLO Conformance

Runs 60 minutes after full production promotion. Final validation gate.

**Checks:**
- **SLO conformance** — all owned SLOs must be at or above target for the 60-minute post-promotion window
- **Error budget impact** — deployment must not have consumed more than 5% of the monthly error budget in 60 minutes
- **Synthetic transaction pass rate** — production synthetic monitors must maintain >99.9% success rate
- **Downstream impact check** — error rates for top 5 downstream consumers of this service must not have increased

**On failure:** PagerDuty P1; automatic rollback initiated via ArgoCD; on-call notified with gate failure details.

---

## Repository Structure

```
deployment-validation-gates/
├── github-actions/
│   ├── gate1-pre-deploy.yaml        # GitHub Actions workflow for Gate 1
│   ├── gate2-staging.yaml           # Staging validation workflow
│   └── gate-failure-notify.yaml    # Failure notification workflow
├── scripts/
│   ├── smoke-tests/
│   │   ├── run-smoke-tests.sh       # Smoke test runner
│   │   └── test-cases/              # Synthetic transaction definitions
│   ├── canary-gate-check.py         # Gate 3 metric comparison logic
│   ├── slo-conformance-check.py     # Gate 4 SLO validation script
│   └── resource-limit-checker.sh   # Kubernetes manifest resource limit enforcer
├── config/
│   ├── gate-thresholds.yaml         # All gate pass/fail thresholds in one place
│   ├── dependency-compatibility.yaml # Service dependency version matrix
│   └── synthetic-monitors.yaml     # Production synthetic monitor definitions
├── pact/
│   ├── consumer-contracts/          # Pact consumer contract definitions
│   └── provider-verification/      # Provider-side Pact verification
└── dashboards/
    └── deployment-health.json       # Deployment gate pass/fail Grafana dashboard
```

---

## Gate Thresholds Configuration

All gate thresholds are centralized in `config/gate-thresholds.yaml` to avoid scattering magic numbers across scripts:

```yaml
gates:
  canary:
    observation_window_minutes: 30
    error_rate_delta_max_pp: 0.1       # percentage points above baseline
    latency_p99_delta_max_ms: 50
    slo_burn_rate_multiplier_max: 2.0
    anomaly_score_max: 0.6

  post_promotion:
    observation_window_minutes: 60
    slo_conformance_required: true
    error_budget_max_consumption_pct: 5.0
    synthetic_pass_rate_min_pct: 99.9
```

Threshold changes require a pull request with SRE review. This prevents emergency "temporarily relaxing" a threshold as a way to force a broken deploy through.

---

## Deployment Failure Reduction

After 12 months of the full four-gate framework:

| Metric | Before | After | Change |
|---|---|---|---|
| Deployment-related P1/P2 incidents | 18/month | 3/month | -83% |
| Mean time from deploy to incident detection | 22 min | 8 min | -64% |
| Automatic rollback success rate | N/A (manual) | 97% | New capability |
| Deployment lead time (merge to prod) | 35 min | 58 min | +23 min |

The 23-minute increase in lead time is the deliberate cost of safety. For a platform processing 50,000+ orders/minute at peak, the tradeoff is unambiguously correct.

---

## Lessons Learned

**Canary gates need business metrics, not just technical metrics.** A deploy can have a healthy error rate and p99 while still degrading the order completion funnel (e.g., subtly broken add-to-cart behavior). Business metric checks at the canary stage catch what technical metrics miss.

**Gate failures must be actionable.** Early versions sent "Gate 3 failed" with a link to a wall of metrics. Engineers didn't know where to start. Gate failure notifications now include: the specific check that failed, the observed value, the threshold, and a direct link to the relevant dashboard or log query.

**Don't set thresholds so tight that every deploy triggers a gate.** Gate fatigue is real. If Gate 3 fires on 30% of deployments due to noise in the baseline comparison, engineers route around it. Calibrate thresholds to catch real regressions, not statistical noise.

---

## Prerequisites

- GitHub Actions
- ArgoCD (for GitOps-based rollback)
- Datadog (for metric-based gate checks)
- Pact Broker (for contract test verification)
- k6 or Locust (for load testing in Gate 2)
