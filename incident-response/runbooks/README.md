# Incident Response Runbooks

A structured library of operational runbooks covering the 20 most common failure modes observed across the digital ordering platform. Each runbook was written or refined after a real incident — not speculatively — and is formatted for rapid use under pressure by an on-call engineer who may be unfamiliar with the specific service.

---

## Background

The most expensive part of an incident is usually the first 10 minutes: the on-call engineer orienting to what's broken, where to look, and what actions are safe to take. Runbooks cut that cost by front-loading the decision tree that an experienced engineer has already worked through.

Before this library was formalized, MTTR averaged 45 minutes. The combination of structured runbooks, improved alerting context (see [`ai-log-anomaly-detector`](../observability/ai-log-anomaly-detector/)), and tighter on-call handoff procedures drove MTTR to **30 minutes** — a 33% improvement — over 12 months.

---

## Runbook Format Standard

Every runbook follows a fixed structure to minimize cognitive overhead during an incident:

```
# [Service/Component] — [Failure Mode]

## Severity
P1 / P2 / P3 — and what makes this severity

## Customer Impact
What the customer actually experiences. Written in plain language.

## Symptoms
- What alerts fired
- What metrics look abnormal and their expected vs. observed values
- Log patterns to search for (exact Datadog query included)

## Immediate Triage (< 5 min)
Numbered steps to confirm the issue and establish scope.
Each step includes the exact command or dashboard link.

## Remediation Options
Ordered by: safest → most disruptive
Each option includes: expected recovery time, risk, and rollback steps.

## Escalation Path
Who to call and when, with contact rotation info.

## Post-Incident Actions
What to create in Jira, what signals to preserve for postmortem.
```

---

## Runbook Index

### Ordering Platform

| Runbook | Severity | Typical MTTR |
|---|---|---|
| [Order Placement API — High Error Rate](ordering-api-high-error-rate.md) | P1 | 8 min |
| [Order Placement API — Latency Spike](ordering-api-latency-spike.md) | P1 | 12 min |
| [Order Queue — Consumer Lag](order-queue-consumer-lag.md) | P2 | 15 min |
| [Menu Service — Stale Cache](menu-service-stale-cache.md) | P2 | 10 min |
| [Mobile API Gateway — 502/504 Surge](api-gateway-502-504.md) | P1 | 7 min |

### Database & Storage

| Runbook | Severity | Typical MTTR |
|---|---|---|
| [RDS — Connection Pool Exhaustion](rds-connection-pool-exhaustion.md) | P1 | 18 min |
| [RDS — Replica Lag > 30s](rds-replica-lag.md) | P2 | 20 min |
| [Redis — Eviction Rate Spike](redis-eviction-spike.md) | P2 | 12 min |
| [ElasticSearch — Cluster Red Status](elasticsearch-cluster-red.md) | P1 | 25 min |

### Infrastructure

| Runbook | Severity | Typical MTTR |
|---|---|---|
| [Kubernetes Node — Not Ready](k8s-node-not-ready.md) | P2 | 10 min |
| [Pod OOMKilled — Recurring](pod-oomkilled-recurring.md) | P2 | 15 min |
| [HPA — Max Replicas Hit](hpa-max-replicas-hit.md) | P1 | 10 min |
| [Cert Expiry — < 7 Days](cert-expiry-warning.md) | P2 | 30 min |
| [DNS Resolution Failures](dns-resolution-failures.md) | P1 | 12 min |

### Integrations & Third Parties

| Runbook | Severity | Typical MTTR |
|---|---|---|
| [Payment Gateway — Timeout Spike](payment-gateway-timeouts.md) | P1 | 20 min |
| [Loyalty Service — Rewards Redemption Failure](loyalty-redemption-failure.md) | P2 | 15 min |
| [Third-Party POS Integration — Sync Delay](pos-integration-sync-delay.md) | P2 | 25 min |

### Observability & CI/CD

| Runbook | Severity | Typical MTTR |
|---|---|---|
| [Datadog Agent — Not Reporting](datadog-agent-not-reporting.md) | P3 | 20 min |
| [Deployment Rollback Procedure](deployment-rollback.md) | P1/P2 | 8 min |
| [Canary — Automatic Rollback Failed](canary-rollback-failed.md) | P1 | 15 min |

---

## Runbook Authorship and Maintenance

**Who writes runbooks:** The on-call engineer who responds to an incident is responsible for either creating a new runbook or updating an existing one within 48 hours of incident close. This is a required postmortem action item.

**Runbook review cadence:** Each runbook is reviewed quarterly. Runbooks that haven't been used in 6 months are marked for archival review — either the failure mode has been eliminated (good) or we've stopped detecting it (bad).

**Runbook testing:** Critical P1 runbooks are walk-tested during chaos engineering exercises. If a runbook's step 3 says "check metric X on dashboard Y" and that dashboard has been renamed, the runbook is wrong. Chaos days surface these mismatches before incidents do.

---

## On-Call Onboarding

New on-call engineers are expected to shadow two incidents and independently work through five runbooks in a non-production environment before taking primary on-call rotation. The most important runbooks to internalize first:

1. [`deployment-rollback.md`](deployment-rollback.md) — you will use this
2. [`rds-connection-pool-exhaustion.md`](rds-connection-pool-exhaustion.md) — the most common P1
3. [`api-gateway-502-504.md`](api-gateway-502-504.md) — highest customer visibility

---

## Severity Reference

| Level | Definition | Response Time | Notification |
|---|---|---|---|
| P1 | Customer-facing ordering or payment flow degraded >1% error rate | 5 min | Page + call |
| P2 | Degraded performance, no complete outage; internal tools affected | 30 min | Page |
| P3 | Minor degradation, workaround exists; no customer impact | 4 hours | Slack ticket |

---

## Tools Referenced in Runbooks

- **Datadog** — metrics, logs, APM traces, SLO dashboards
- **PagerDuty** — incident creation and escalation
- **kubectl** — Kubernetes cluster operations
- **AWS Console / CLI** — RDS, EKS, SQS, CloudFront operations
- **ArgoCD** — deployment status and rollback
- **Slack #incidents** — real-time incident coordination channel
