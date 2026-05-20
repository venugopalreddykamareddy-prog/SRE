# SRE Portfolio — Venu Gopal Reddy Kamareddy

9 years doing SRE and platform engineering, mostly in retail and e-commerce. Most of this work came out of supporting Starbucks' digital ordering platform — high-volume, customer-facing, with enough complexity to keep things interesting. This repo captures the tooling and patterns I've built or refined over that time.

Not everything here is perfect. Some of it is still evolving. That's kind of the point.

---

## What's in here

| Work | What it does |
|---|---|
| Datadog SLO framework | Burn-rate alerting across services, wired to PagerDuty |
| MTTR reduction | Runbooks + automation that brought mean resolution time down ~30 min |
| Deployment validation gates | Pre/post-deploy checks that caught most rollback-worthy deployments before they fully rolled out |
| RBAC guardrails | Came out of a real privilege escalation incident — baseline policy enforcement now runs as a daily CronJob |
| Chaos engineering | Scheduled fault injection experiments; found a few failure modes we hadn't accounted for |
| OpenTelemetry migration | Moved off a vendor-proprietary pipeline to reduce lock-in |

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

## How I think about this work

I've found that most reliability problems aren't technical — they're process problems wearing a technical costume. The SLO burn-rate work isn't valuable because of the math; it's valuable because it gives product and engineering a shared language for talking about reliability trade-offs. Same with chaos experiments: the point isn't the experiment itself, it's getting teams to articulate their assumptions before something breaks them in production.

A few things I keep coming back to:

- Error budgets make reliability conversations easier. Once a team owns a budget, the question stops being "was this outage bad?" and starts being "are we burning budget faster than we're shipping value?"
- Blameless postmortems only work if leadership actually believes them. The template is easy. The culture isn't.
- Automate toil early. I've seen teams spend years manually rotating credentials or restarting pods on schedule. That time compounds.
- Chaos testing is uncomfortable and that's the point. Teams that resist it are usually the ones who need it most.

---

## Production Incident Example

**Payment API latency spike — morning rush**

We started seeing elevated p99 latency (~2.3s) on the payment service about 20 minutes after a routine deployment. No alert fired immediately because the SLO burn rate was slow enough to miss the fast-burn threshold in the first window.

**Investigation:**
- Pulled Datadog APM traces — saw DB query time climbing, not app processing time
- Checked RDS connection count — sitting at 98% pool utilization
- Compared pod memory before/after deploy — new version had a connection leak in the retry logic
- Confirmed with `kubectl top pods` that memory was growing linearly post-deploy

**Fix:**
- Rolled back the deployment via Argo Rollouts
- Patched the connection leak in retry handler (see `rollback_handler.go`)
- Updated readiness probe to catch connection pool exhaustion before traffic shifted

**Result:** Latency dropped from ~2.3s → ~380ms within 4 minutes of rollback. Total customer impact window: ~35 minutes.

Captured outputs from this incident:
- [`examples/kubectl-incident-snapshot.txt`](examples/kubectl-incident-snapshot.txt) — pod memory growth and connection pool logs that confirmed the leak
- [`examples/canary-gate-fail-rollback.txt`](examples/canary-gate-fail-rollback.txt) — gate evaluation output that triggered the rollback
- [`examples/slo-burn-rate-report.txt`](examples/slo-burn-rate-report.txt) — burn rate report showing payment-api at 8.8x during the 6h window

**What this exposed:**
- Our 1h burn-rate window missed it. We added a tighter 15-minute fast-burn check for payment services specifically.
- The readiness probe wasn't testing anything meaningful. Fixed that.

---

## Lessons Learned

Some things I'd approach differently:

- **Alert fatigue is real.** We had a period where too many P3 alerts were firing on every deploy. Engineers started ignoring them. Fewer, higher-confidence alerts are better than comprehensive noisy ones.
- **SLO thresholds need regular review.** The numbers I picked initially were reasonable guesses. After 6 months of production data they needed adjustment. Build that review into a quarterly process, not a one-time setup.
- **Terraform state management gets messy fast.** Especially across multiple teams and accounts. I've gotten better at workspace isolation but there are still rough edges in this repo.
- **Runbooks rot.** The ones in this repo are maintained, but I've seen runbooks that were 2 years out of date and actively misleading. Tie runbook reviews to postmortems and they stay current.
- **The chaos experiments that find nothing are still useful** — they validate assumptions. The ones that find something are just more dramatic.

---

## Cloud & Tooling Stack

**Cloud:** AWS (EKS, RDS, SQS, CloudWatch), Azure (AKS, Azure Monitor, Event Hubs), GCP (GKE, Cloud Spanner, Pub/Sub)

**Observability:** Datadog, OpenTelemetry, Grafana, Prometheus

**Infrastructure:** Terraform, Helm, Kubernetes

**CI/CD:** GitHub Actions, Jenkins

**Incident Management:** PagerDuty, Slack, Jira

**Chaos Engineering:** LitmusChaos, AWS Fault Injection Simulator

---

## Domain Context

Retail at scale has some specific reliability challenges that I don't see talked about much:

- **Morning rush is predictable but still hard.** You know it's coming, but traffic ramps faster than most autoscalers react. Pre-warming matters.
- **POS and payment integrations are fragile.** Third-party dependencies with their own undocumented failure modes. Circuit breakers and aggressive timeouts are non-negotiable.
- **Loyalty/rewards must not block ordering.** The dependency isolation work came from a real incident where a rewards service timeout cascaded into order failures.
- **Multi-region failover is only real if you test it.** We had failover configured for a year before we actually ran a test. It didn't work the way we thought.

---

## Sample Outputs

Real output from these tools — useful for understanding what they actually produce:

| File | What it shows |
|---|---|
| [`examples/canary-gate-fail-rollback.txt`](examples/canary-gate-fail-rollback.txt) | Gate failure triggering a rollback during the payment-api incident |
| [`examples/canary-gate-pass.txt`](examples/canary-gate-pass.txt) | Clean two-stage canary promotion |
| [`examples/slo-burn-rate-report.txt`](examples/slo-burn-rate-report.txt) | Burn rate report across 4 services, one in P2 alert state |
| [`examples/rbac-audit-violations.txt`](examples/rbac-audit-violations.txt) | RBAC audit catching a leftover cluster-admin binding from a migration job |
| [`examples/kubectl-incident-snapshot.txt`](examples/kubectl-incident-snapshot.txt) | kubectl output during the payment-api connection pool incident |
| [`examples/log-triage-sample.txt`](examples/log-triage-sample.txt) | AI log triage classifying P1 errors with remediation steps |

---

## Getting Started

Each subdirectory has a `README.md` with more detail. Start wherever is most relevant:

- New to SLOs? → [`observability/datadog-slo-framework/`](observability/datadog-slo-framework/README.md)
- Investigating an incident? → [`incident-response/runbooks/`](incident-response/runbooks/README.md)
- Improving deployment safety? → [`cicd-reliability/deployment-validation-gates/`](cicd-reliability/deployment-validation-gates/README.md)
- Building resilience? → [`chaos-engineering/chaos-experiments/`](chaos-engineering/chaos-experiments/README.md)

---

*Configurations are anonymized for public sharing. No production credentials or internal hostnames.*
