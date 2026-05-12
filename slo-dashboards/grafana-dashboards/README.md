# Grafana SLO Dashboards

A suite of Grafana dashboards providing SLO/SLA visibility across the multi-cloud ordering platform — from executive-level reliability summaries to deep-dive per-service SLO burn rate analysis. Built with a single design principle: every dashboard should answer a specific question in under 30 seconds.

---

## Background

Observability dashboards often fail the people who need them most. Executive dashboards are too vague to act on. Engineering dashboards are too dense to read under pressure. The result: on-call engineers build their own ad-hoc queries during incidents (slow), and leadership has no clear picture of systemic reliability trends (uninformed decisions).

This dashboard suite was designed to serve three audiences with distinct needs:

- **Leadership** — Is the platform reliably serving customers? Are we trending toward or away from our SLA commitments?
- **SRE on-call** — Which service is burning error budget right now, and how fast?
- **Service engineers** — How is my service performing against its SLO over time, and where is latency coming from?

---

## Dashboard Inventory

### Executive & SLA Dashboards

**`sla-executive-summary`**
Audience: VPs, Directors, SRE leadership

Answers: Are we meeting our customer commitments? Are we trending toward SLA breach?

Key panels:
- Platform-wide availability (30-day rolling, vs. 99.99% SLA target)
- Services with >50% error budget consumed this month (risk signal)
- Monthly SLO achievement rate by service tier (table)
- Rolling 90-day MTTR trend
- Incident count by severity (month-over-month)

Refresh: 5 minutes | Time range default: Last 30 days

---

**`error-budget-overview`**
Audience: SRE team, Engineering managers

Answers: How much reliability runway do we have left this month? Which services are at risk?

Key panels:
- Error budget remaining per service (% remaining, color-coded: green/yellow/red)
- Budget burn rate (current 1h vs. 6h window for multi-window alert context)
- Projected budget exhaustion date (if current burn rate continues)
- Top 5 services by budget consumption this period

Refresh: 1 minute | Time range default: Current month

---

### On-Call Operations Dashboards

**`slo-burn-rate-live`**
Audience: On-call SRE engineers

Answers: Is anything burning error budget right now? Where should I look first?

Key panels:
- All services sorted by current burn rate (highest → lowest)
- Multi-window burn-rate comparison (1h, 6h, 72h) per service
- Active SLO alerts (currently firing monitors)
- Recent SLO events timeline (when burn rate thresholds were crossed in the last 24h)

Refresh: 30 seconds | Time range default: Last 3 hours

---

**`incident-impact-tracker`**
Audience: Incident commander, on-call SRE

Answers: What is the real-time impact of the current incident on SLOs and error budgets?

Key panels:
- Error rate and latency for the incident-relevant service (current vs. 7-day baseline)
- Error budget consumed since incident start (running counter)
- Dependency health heatmap (are downstream services affected?)
- Estimated MTTR contribution at current error rate

Refresh: 15 seconds | Time range default: Incident start time to now

---

### Service-Level Dashboards

**`service-slo-detail`** (parameterized, one per service team)
Audience: Service engineering teams

Answers: How is my service performing against its SLO? Where is the latency coming from?

Key panels:
- Availability SLO gauge (current compliance % vs. target, 30-day rolling)
- Latency SLO compliance (% of requests within threshold, p50/p95/p99 time series)
- Error rate by endpoint (heatmap showing which endpoints are contributors)
- SLO compliance calendar (heatmap of daily compliance — spot patterns)
- Dependency latency breakdown (how much of my p99 is my code vs. downstream calls)
- Recent deployments (overlaid on the time-series to correlate changes with degradations)

Service is selected via a dashboard variable — one dashboard definition serves all services.

Refresh: 1 minute | Time range default: Last 7 days

---

**`slo-trend-analysis`**
Audience: Service engineers, quarterly SLO review

Answers: Is our reliability improving over time? Are we meeting our SLO targets quarter-over-quarter?

Key panels:
- 90-day SLO compliance trend per service
- MTTR rolling average (trailing 90 days)
- Deployment frequency vs. incident frequency (are more deploys correlating with incidents?)
- Mean time to detect (MTTD) trend
- Error budget consumption comparison: this month vs. last 3 months

Refresh: 1 hour | Time range default: Last 90 days

---

## Repository Structure

```
grafana-dashboards/
├── dashboards/
│   ├── sla-executive-summary.json       # Executive SLA summary dashboard
│   ├── error-budget-overview.json       # Error budget consumption dashboard
│   ├── slo-burn-rate-live.json          # Real-time burn rate for on-call
│   ├── incident-impact-tracker.json     # Live incident impact dashboard
│   ├── service-slo-detail.json          # Per-service SLO detail (parameterized)
│   └── slo-trend-analysis.json          # Quarterly SLO trend dashboard
├── provisioning/
│   ├── grafana-datasources.yaml         # Grafana datasource provisioning config
│   ├── grafana-dashboards.yaml          # Dashboard auto-provisioning config
│   └── alerting-rules.yaml             # Grafana alerting rules (backup to Datadog)
├── terraform/
│   └── grafana-resources.tf            # Grafana folders, datasources via Terraform
├── scripts/
│   ├── import-dashboards.sh            # Bulk import via Grafana HTTP API
│   ├── export-dashboards.sh            # Export current dashboards to JSON
│   └── validate-dashboards.sh         # Validate JSON structure before import
└── variables/
    └── service-list.yaml               # Master list of services for dashboard variables
```

---

## Dashboard Design Standards

All dashboards in this library follow these conventions:

**Color coding is consistent and meaningful:**
- Green: SLO target met / healthy
- Yellow: Warning (50–80% error budget consumed, or burn rate 2–5×)
- Red: Critical (>80% budget consumed, or burn rate >5×, or SLO breached)
- Same colors appear in the same meaning across all dashboards.

**Every panel has a description:**
Hovering the `i` icon on any panel shows: what the panel measures, how to interpret it, and what action it implies. Dashboard panels without descriptions get removed at review.

**Thresholds match alert thresholds:**
If the burn-rate alert fires at 6× for a slow-burn, the dashboard panel for burn rate has a threshold line at 6×. If a dashboard shows a value that doesn't have a corresponding alert, either the alert is missing or the panel is noise.

**Consistent time zones:**
All dashboards display UTC. All annotations (deployments, incidents) are in UTC. When local time is needed, it is shown as a secondary label, not the primary.

---

## Datasource Configuration

Dashboards use three data sources:

| Source | Type | Purpose |
|---|---|---|
| Datadog | Datadog plugin | SLO metrics, burn rates, APM latency |
| Prometheus | Prometheus | Infrastructure metrics (Kubernetes, node-level) |
| Loki | Loki | Log-based metrics and error pattern panels |

All datasources are provisioned via code (`provisioning/grafana-datasources.yaml`). No manual datasource creation via UI.

---

## Deployment & Provisioning

Dashboards are provisioned via Grafana's dashboard provisioning feature on startup. To deploy or update:

```bash
# Import a single updated dashboard
./scripts/import-dashboards.sh dashboards/slo-burn-rate-live.json

# Import all dashboards (idempotent)
./scripts/import-dashboards.sh --all

# Export current dashboard state (before making edits in UI)
./scripts/export-dashboards.sh --folder SRE-SLOs

# Validate all JSON before deployment
./scripts/validate-dashboards.sh
```

Changes to dashboards should be exported to JSON and committed via PR. Ad-hoc UI edits that are not committed are lost on the next provisioning run — intentionally. All dashboards are owned by the repository, not by individual Grafana users.

---

## Linking Dashboards to Alerts

Every Datadog SLO alert links to the relevant Grafana dashboard panel for the service. This is enforced in the alert template:

```
Grafana: {{ .dashboard_url }}?var-service={{ .service }}&from={{ .incident_start }}
```

When an on-call engineer receives a page, the Grafana dashboard is one click away, pre-filtered to the relevant service and time window. Reducing the "what am I looking at?" time at incident start is how MTTR improves.

---

## Lessons Learned

**Dashboards are documentation — keep them curated.** At peak, the team had 47 Grafana dashboards. Nobody could find anything. A curation pass reduced it to 12 high-quality, well-maintained dashboards. The rest were either redundant, stale, or personal debugging views that belonged in a personal folder, not the shared SRE space.

**If a panel doesn't drive an action, remove it.** "Nice to have" panels create cognitive load. Every panel should have a clear story: if the value is X, do Y. If you can't say what Y is, the panel is informational noise.

**Show deployments as annotations on every time-series.** The single highest-value change ever made to the dashboards: overlaying deployment events as vertical lines on metric time-series. Latency regression + deployment line at the same timestamp = 30 seconds to root cause. Without the deployment annotation: 20 minutes of investigation.

---

## Prerequisites

- Grafana 10.x+
- Grafana Datadog plugin (for Datadog metric queries)
- Prometheus (for infrastructure metrics)
- Loki (for log-based metrics)
- Grafana API key (for scripted import/export)
- Terraform >= 1.3 (for managed provisioning)
