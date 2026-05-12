# Datadog SLO Framework

A production-grade SLO framework built on Datadog for tracking and enforcing reliability targets across multi-cloud microservice architectures. Designed during my time supporting Starbucks' digital ordering platform — a system where missing an SLO meant real customers couldn't place orders at their local store.

---

## Background

Early in the observability journey at scale, the team had dashboards showing uptime percentages, but no mechanism to translate those numbers into actionable engineering priorities. A service could be "99% up" and still be silently degrading for a customer segment. We needed a framework that:

1. Defined reliability in terms customers actually experience (latency, error rate, checkout completion rate)
2. Made burn-rate visible before the month's error budget was exhausted
3. Created a shared language between SRE and product engineering around release risk

This framework is the result of three iterations built over 18 months.

---

## SLO Design Principles

### Measure what the customer experiences, not what's easy to instrument

Bad SLI: "Is the service pod running?"
Good SLI: "Did the order placement API respond successfully within 500ms?"

Every SLO in this framework is anchored to a user-facing interaction: browsing the menu, adding items to cart, placing an order, redeeming rewards, receiving an order confirmation.

### Error budgets are a product conversation, not just an ops metric

Error budget = `(1 - SLO target) × rolling window duration`

When a service consumes 50% of its monthly error budget in a single week, that is a signal for the product team to pause new feature work and invest in reliability. The framework automates this conversation by feeding budget burn into weekly engineering reviews.

### Multi-window burn-rate alerts catch both fast burns and slow bleeds

A single-threshold alert misses gradual degradations. This framework implements:

- **Fast burn** (1h window, 14× budget consumption rate) — page immediately
- **Slow burn** (6h window, 6× rate) — ticket + Slack notification
- **Budget warning** (72h window, 3× rate) — engineering review trigger

---

## Framework Structure

```
datadog-slo-framework/
├── slo-definitions/
│   ├── ordering-api-slo.yaml          # Core order placement SLO
│   ├── menu-service-slo.yaml          # Menu browsing availability SLO
│   ├── loyalty-service-slo.yaml       # Rewards redemption SLO
│   └── payment-gateway-slo.yaml       # Payment completion SLO
├── monitors/
│   ├── fast-burn-alerts.yaml          # 1h and 5m window burn monitors
│   ├── slow-burn-alerts.yaml          # 30m and 6h window burn monitors
│   └── budget-warning-monitors.yaml   # Proactive budget depletion warnings
├── dashboards/
│   ├── executive-slo-summary.json     # High-level reliability status for leadership
│   └── service-slo-detail.json        # Per-service SLO drilldown for engineers
├── scripts/
│   ├── create-slos.sh                 # Idempotent SLO provisioning via Datadog API
│   ├── export-budget-report.py        # Monthly error budget consumption report
│   └── slo-audit.sh                   # Validates all SLOs match definitions on file
└── terraform/
    └── slo-resources.tf               # Terraform-managed SLO and monitor resources
```

---

## SLO Targets Reference

| Service | SLI Type | Target | Window | Error Budget/Month |
|---|---|---|---|---|
| Order Placement API | Availability + Latency p99 < 500ms | 99.95% | 30 days | 21.6 min |
| Menu Service | Availability | 99.99% | 30 days | 4.3 min |
| Loyalty/Rewards | Availability + Latency p95 < 800ms | 99.9% | 30 days | 43.2 min |
| Payment Gateway | Success rate (non-4xx) | 99.95% | 30 days | 21.6 min |
| Mobile App API Gateway | Availability + p99 < 1s | 99.9% | 30 days | 43.2 min |

---

## Burn-Rate Alert Configuration

The multi-window burn-rate model is derived from Google's SRE Workbook. Two conditions must both be true to fire a page:

```yaml
# Fast burn — wakes someone up
alert_condition:
  short_window: 1h
  long_window: 5m
  burn_multiplier: 14.4
  effect: "Consumes 2% of monthly budget in 1 hour"
  action: PagerDuty P1 page

# Slow burn — creates a ticket
alert_condition:
  short_window: 6h
  long_window: 30m
  burn_multiplier: 6.0
  effect: "Consumes 5% of monthly budget in 6 hours"
  action: Jira ticket + Slack #sre-alerts
```

---

## Integration with Release Process

The error budget drives release decisions through three gates:

1. **Green (>50% budget remaining)**: Full velocity, normal deployment cadence
2. **Yellow (20–50% remaining)**: Deployments require SRE review and rollback plan
3. **Red (<20% remaining)**: Feature deployments frozen; reliability work only

This gate is enforced automatically in the CI/CD pipeline via a Datadog API check before promotion to production.

---

## Lessons Learned

**Don't set SLO targets by committee before you have baseline data.** The first version of this framework set targets aspirationally. Several services couldn't meet their own SLOs on day one, which eroded trust in the framework. The second iteration started by measuring 30 days of actual performance and setting SLOs at the 95th percentile of observed reliability — then tightening over time.

**Latency SLOs catch what availability SLOs miss.** A service returning 200s in 3 seconds looks "available" but is functionally broken for a mobile checkout flow. Latency gates at p95 and p99 became as important as error rate for user-facing services.

**Alert on burn rate, not on the SLO percentage directly.** A 99.94% day on a 99.95% SLO is fine. A 99.94% day after three consecutive 99.94% days means you're heading toward a miss. Burn rate catches the trajectory.

---

## Prerequisites

- Datadog account with SLO and Monitor APIs enabled
- `DD_API_KEY` and `DD_APP_KEY` environment variables
- Terraform >= 1.3 (for managed provisioning)
- Python 3.9+ (for budget reporting scripts)
