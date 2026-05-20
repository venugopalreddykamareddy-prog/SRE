# SRE Portfolio — Venu Gopal Reddy Kamareddy

Senior Site Reliability Engineer with 9+ years of experience designing and operating mission-critical distributed systems across AWS, Azure, and GCP. This portfolio captures real-world SRE patterns, tooling, and frameworks built while supporting Starbucks' retail and digital ordering platforms — systems that process millions of transactions daily with a target of **99.99% uptime**.

---

## Impact Highlights

| Initiative | Outcome |
|---|---|
| Datadog SLO observability framework | Unified reliability visibility across 50+ microservices and three cloud providers |
| MTTR reduction program | Mean time to resolution improved from **45 min → 30 min** (33% improvement) |
| CI/CD deployment validation gates | Deployment failures reduced by **85%** across production pipelines |
| RBAC guardrails (post-Azure outage) | Blast-radius controls that prevented repeat privilege escalation incidents |
| Chaos engineering program | Proactively surfaced 12 latent failure modes before they caused customer impact |
| OpenTelemetry pipeline migration | Eliminated vendor lock-in, reduced observability cost by 30% |

---

## Repository Structure

```
sre-portfolio/
├── observability/
│   ├── datadog-slo-framework/      # SLO/SLA tracking with burn-rate alerts
│   ├── opentelemetry-pipeline/     # Vendor-agnostic trace/metric/log pipeline
│   └── ai-log-anomaly-detector/    # ML-based anomaly detection for log streams
│
├── incident-response/
│   ├── runbooks/                   # Structured runbooks for top 20 failure modes
│   ├── rbac-guardrails/            # RBAC policies built after Azure outage recovery
│   └── postmortem-template/        # Blameless postmortem process and templates
│
├── cicd-reliability/
│   ├── deployment-validation-gates/ # Pre/post-deploy quality gates
│   └── canary-rollout/             # Progressive delivery with automated rollback
│
├── chaos-engineering/
│   └── chaos-experiments/          # Controlled failure injection experiments
│
└── slo-dashboards/
    └── grafana-dashboards/         # SLO/SLA Grafana dashboard definitions
```

---

## Core SRE Philosophy

**Reliability is a feature, not an afterthought.** Every system in this portfolio is designed around the principle that operational excellence must be built in from day one — not bolted on after the first outage.

Key tenets applied throughout this work:

- **Error budgets over uptime theater** — SLOs are negotiated with product teams and enforced through burn-rate alerts, not ad-hoc on-call escalations. A breach of the error budget is a conversation with product, not just a page to engineering.
- **Observability before monitoring** — Systems are instrumented for the unknown, not just the anticipated. OpenTelemetry traces, structured logs, and business-level metrics are first-class requirements.
- **Blameless culture** — Postmortems focus on systemic fixes. Blame produces silence; analysis produces resilience.
- **Automate the toil** — Any manual remediation step that runs more than twice gets automated. This is how MTTR drops from 45 to 30 minutes.
- **Test your assumptions** — Chaos experiments are scheduled quarterly. If a failover has never been tested under real conditions, you don't actually know if it works.

---

## Cloud & Tooling Stack

**Cloud:** AWS (EKS, RDS, SQS, CloudWatch), Azure (AKS, Azure Monitor, Event Hubs), GCP (GKE, Cloud Spanner, Pub/Sub)

**Observability:** Datadog, OpenTelemetry, Grafana, Prometheus

**Infrastructure:** Terraform, Helm, Kubernetes

**CI/CD:** GitHub Actions, Jenkins

**Incident Management:** PagerDuty, Slack, Jira

**Chaos Engineering:** LitmusChaos, AWS Fault Injection Simulator

---

## Domain Context — Starbucks-Scale Retail SRE

Supporting a global retail brand at scale means reliability failures are immediately customer-visible and revenue-impacting. The digital ordering platform alone processes peaks of **50,000+ orders per minute** during morning rush. Key operational challenges addressed in this portfolio:

- **Multi-region active-active failover** — No single region can be a single point of failure during a global store opening wave.
- **Dependency isolation** — A failure in the loyalty/rewards service must not cascade to the order placement flow. Circuit breakers and fallback paths are non-negotiable, not nice-to-haves.
- **Release velocity without reliability regression** — Engineering teams deploy 15–20 times per week. Keeping that cadence without increasing incident rate is the core challenge the CI/CD reliability work addresses.
- **Third-party payment and POS integration** — External dependencies with their own SLAs require defensive timeout and circuit-breaker patterns throughout.

---

## Getting Started

Each subdirectory contains a self-contained `README.md` with context, design rationale, and implementation details. Start with the areas most relevant to your use case:

- New to SLOs? → [`observability/datadog-slo-framework/`](observability/datadog-slo-framework/README.md)
- Investigating an incident? → [`incident-response/runbooks/`](incident-response/runbooks/README.md)
- Improving deployment safety? → [`cicd-reliability/deployment-validation-gates/`](cicd-reliability/deployment-validation-gates/README.md)
- Building resilience? → [`chaos-engineering/chaos-experiments/`](chaos-engineering/chaos-experiments/README.md)

---

*All configurations are anonymized and sanitized for public sharing. No production credentials, API keys, or internal hostnames are present in this repository.*
