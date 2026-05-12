# Chaos Engineering Experiments

A structured chaos engineering program for proactively exposing hidden failure modes in a mission-critical retail ordering platform before they cause customer impact. These experiments follow the principles of controlled failure injection: define a steady state, form a hypothesis, run the experiment in a limited blast radius, and act on the findings — every time.

---

## Background

The shift from reactive incident response to proactive resilience testing came after a particularly painful incident: a downstream payment service went down, and the ordering service — which had no circuit breaker — continued trying to call the payment service for 8 minutes, exhausting its own connection pool and taking itself down in the process. The ordering service had been "available" for 18 months without a major incident. The assumption that it was resilient was never tested.

The chaos engineering program was started the week after that postmortem. The first experiment: kill the payment service in staging and observe ordering's behavior. It failed exactly the same way. We had the answer in 20 minutes instead of during a production incident.

Since establishing the quarterly chaos days, the program has proactively surfaced **12 latent failure modes** — cascades, timeout misconfigurations, cache stampede vulnerabilities, and stale circuit breaker settings — none of which caused a production incident.

---

## Experiment Design Principles

### Measure steady state before injecting failure

Every experiment begins with a 10-minute baseline measurement of error rate, latency, and business metrics. A baseline is the only way to know whether the chaos you injected actually changed anything — or whether the system was already degraded before you touched it.

### Form a hypothesis with a specific, measurable prediction

Bad hypothesis: "The system should handle database failures gracefully."

Good hypothesis: "When we kill 1 of 3 RDS read replicas, p99 read latency will increase by less than 200ms, error rate will remain below 0.5%, and the service will route queries to the remaining replicas within 30 seconds."

The hypothesis must be falsifiable. If there's no way to prove it wrong, it's not a hypothesis.

### Start small and expand blast radius deliberately

Always start with the minimum viable perturbation: 1 pod, 1 availability zone, 1 replica. Only expand if the system handles the small case and you want to verify the larger one.

### Abort conditions are non-negotiable

Define abort conditions before the experiment begins. If a specific metric exceeds a threshold, the experiment stops immediately and the system is restored. Never override an abort condition mid-experiment because "it's almost done."

---

## Experiment Library

### Infrastructure Resilience

| Experiment | Target | Hypothesis | Status |
|---|---|---|---|
| [Single AZ failure](experiments/az-failover.md) | EKS node pool | Traffic fails over to 2 remaining AZs within 60s | Validated |
| [Node termination](experiments/node-termination.md) | Random EKS node | Pod rescheduling completes within 3 min, no SLO breach | Validated |
| [Network partition](experiments/network-partition.md) | Service mesh | Circuit breakers open; fallback response returned | Regression found |
| [DNS resolution failure](experiments/dns-failure.md) | CoreDNS | Service uses cached DNS until resolution restores | Validated |
| [CPU saturation](experiments/cpu-saturation.md) | Ordering API pods | HPA scales out before p99 exceeds SLO | Validated |
| [Memory pressure](experiments/memory-pressure.md) | Menu service | OOMKilled pods restart without cascade | Regression found |

### Database & Cache Resilience

| Experiment | Target | Hypothesis | Status |
|---|---|---|---|
| [RDS read replica kill](experiments/rds-replica-kill.md) | 1 of 3 RDS replicas | Query routing reroutes within 30s; latency < +200ms | Validated |
| [RDS primary failover](experiments/rds-primary-failover.md) | RDS primary | Writes fail briefly; service recovers within 90s | Validated |
| [Redis node eviction](experiments/redis-eviction.md) | Redis cache cluster | Cache miss spike handled; DB not overwhelmed | Regression found |
| [Cache stampede simulation](experiments/cache-stampede.md) | Redis + RDS | Mutex prevents thundering herd on cache expiry | Validated |
| [Connection pool exhaustion](experiments/connection-pool-exhaustion.md) | RDS connections | Circuit breaker trips before cascading to callers | Validated |

### Dependency Failures

| Experiment | Target | Hypothesis | Status |
|---|---|---|---|
| [Payment service latency](experiments/payment-latency.md) | Downstream payment API | Timeout fires at 2s; order queued for retry | Validated |
| [Payment service kill](experiments/payment-kill.md) | Downstream payment API | Circuit breaker opens; user shown graceful error | Regression found |
| [Loyalty service degradation](experiments/loyalty-degradation.md) | Rewards API | Rewards skipped; order still completes | Validated |
| [Third-party POS timeout](experiments/pos-timeout.md) | POS sync service | Sync delayed; no customer-facing impact | Validated |

### Traffic & Load Scenarios

| Experiment | Target | Hypothesis | Status |
|---|---|---|---|
| [Traffic spike 3×](experiments/traffic-spike-3x.md) | Ordering API | HPA handles; p99 stays within SLO | Validated |
| [Sudden traffic drop](experiments/traffic-drop.md) | All services | Scale-down doesn't over-terminate replicas | Validated |
| [Slow consumer](experiments/slow-consumer.md) | Order queue consumer | Queue depth grows; no memory leak in consumer | Regression found |

---

## "Regression Found" Experiments — What We Fixed

Each regression-found experiment represents a real latent failure mode:

**Network partition (fixed: circuit breaker timeout misconfiguration)**
The circuit breaker was configured with `halfOpenAfter: 60s` — 60 seconds was too long. During a 90-second network partition, the circuit breaker never transitioned to half-open, meaning service recovered from the network partition but the circuit stayed open for an extra 30 seconds. Fixed to `halfOpenAfter: 15s`.

**Memory pressure (fixed: missing resource limits on menu-service)**
The menu-service container had requests but no limits. Under memory pressure, it expanded to consume available node memory and triggered OOMKill on co-located pods — not just itself. Added limits; configured soft eviction threshold.

**Redis eviction (fixed: cache stampede on eviction)**
When Redis evicted 30% of keys simultaneously under memory pressure, every service tried to rebuild the cache from the database at once. The DB connection pool exhausted in 45 seconds. Added Lua-script-based cache mutex and staggered TTLs. Cache stampede simulation now validates the fix quarterly.

**Payment service kill (fixed: missing circuit breaker on ordering → payment)**
This was the original incident that started the chaos program. After the circuit breaker was added, this experiment was the first to validate the fix. It now passes every quarter.

**Slow consumer (fixed: consumer lag alarm threshold too high)**
The consumer lag alert was set at 10,000 messages. The experiment showed that at a slow-consumer rate, customer orders were delayed by 8 minutes before the alert fired. Threshold reduced to 500 messages; consumer health now checked every 30 seconds.

---

## Experiment Execution Process

### Quarterly Chaos Day

A 4-hour structured event held once per quarter:

1. **Pre-event (1 week prior)**: Review experiment backlog, select 4–6 experiments, brief participating service owners
2. **Morning session (2 hours)**: Run 3 experiments with full team observation; debrief after each
3. **Afternoon session (2 hours)**: Run 3 experiments; compile findings
4. **Follow-up (within 1 week)**: All regression findings converted to Jira reliability tickets with P2 priority

### Ad-hoc Experiments

Any SRE can run a validated experiment from the library at any time in non-production environments. Running a new experiment in production requires:

1. Hypothesis documented
2. Abort conditions defined
3. Rollback procedure ready
4. At least one other SRE as observer
5. Time window confirmed not during peak traffic (avoid 7–10 AM, 12–2 PM, 5–8 PM)

---

## Repository Structure

```
chaos-experiments/
├── experiments/                        # Individual experiment definitions
│   ├── az-failover.md
│   ├── rds-replica-kill.md
│   ├── payment-kill.md
│   └── ... (one file per experiment)
├── tooling/
│   ├── litmus/
│   │   ├── node-drain-experiment.yaml  # LitmusChaos CRD for node drain
│   │   └── pod-kill-experiment.yaml    # LitmusChaos CRD for pod kill
│   ├── aws-fis/
│   │   ├── az-impairment-template.json # AWS FIS experiment template
│   │   └── rds-failover-template.json  # AWS FIS RDS failover experiment
│   └── scripts/
│       ├── inject-latency.sh           # tc-based network latency injection
│       ├── exhaust-connections.py      # Connection pool exhaustion script
│       └── fill-cache.py              # Cache fill/eviction simulation
├── steady-state/
│   └── baseline-metrics.yaml          # Steady-state metric definitions per service
├── reports/
│   ├── chaos-day-2025-q1.md           # Quarterly chaos day findings report
│   ├── chaos-day-2025-q2.md
│   └── regression-tracker.md         # All regressions found and remediation status
└── runbooks/
    └── experiment-abort-procedures.md  # How to safely abort any experiment
```

---

## Abort Conditions Reference

All experiments share these global abort conditions:

| Condition | Threshold | Action |
|---|---|---|
| Order placement error rate | > 2% for 2 consecutive minutes | Immediate abort + restore |
| Payment success rate | < 98% | Immediate abort + restore |
| SLO error budget consumed | > 10% of monthly budget | Immediate abort |
| On-call paged for unrelated P1 | Any | Pause experiment; assess |
| Experiment executor loses observability | Dashboard/tool failure | Abort |

---

## Lessons Learned

**Never run chaos experiments during peak traffic.** The experiment's failure modes and the peak traffic's failure modes compound. You can't tell what caused what, and the customer impact is real.

**An experiment that always passes isn't being run aggressively enough.** If every experiment in the library passes every quarter with no findings, either the system is genuinely very resilient (verify this by increasing blast radius) or the experiments aren't challenging real assumptions (expand the experiment library).

**The value isn't in the experiments that pass — it's in the ones that find something.** Celebrate regressions. A regression found in chaos day is a production incident that didn't happen.

---

## Prerequisites

- LitmusChaos 3.x (Kubernetes-native fault injection)
- AWS Fault Injection Simulator (for AWS resource-level experiments)
- Datadog (for steady-state measurement and abort condition monitoring)
- kubectl access to target clusters
- Istio (for network partition and latency experiments via traffic policies)
