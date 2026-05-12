# OpenTelemetry Pipeline

A vendor-agnostic observability pipeline built on the OpenTelemetry Collector that unifies traces, metrics, and logs across a polyglot microservice architecture. Originally built to eliminate observability vendor lock-in and reduce per-signal ingestion costs while supporting the full distributed tracing depth needed to diagnose latency in a multi-hop retail ordering flow.

---

## Background

When the digital ordering platform grew beyond 150 services spread across three cloud providers, the observability stack became a patchwork: Datadog agents on AWS, Azure Monitor on Azure, custom Stackdriver shims on GCP, and proprietary SDKs embedded in every service. Adding a new backend or adjusting sampling rates required touching dozens of services individually.

The decision to standardize on OpenTelemetry was driven by three factors:

1. **Cost** — proprietary agent-per-service models scaled linearly with service count; OTel Collector's pipeline processing reduced ingest volume by 30% through tail-based sampling and aggregation.
2. **Portability** — instrumenting once with OTel SDK meant switching or adding a backend (e.g., adding Grafana Tempo alongside Datadog) required only a Collector config change, not a code change.
3. **Depth** — the ordering flow spans 12+ services across two clouds during a single checkout. W3C TraceContext propagation through OTel gave end-to-end traces that no single vendor's proprietary agent could provide alone.

---

## Architecture

```
Application Services (OTel SDK)
        │
        ▼ OTLP/gRPC (port 4317)
┌─────────────────────────────────┐
│     OTel Collector (Gateway)    │
│  ┌────────────────────────────┐ │
│  │  Receivers                 │ │
│  │  - OTLP (gRPC + HTTP)      │ │
│  │  - Prometheus scrape       │ │
│  │  - Fluent Forward (logs)   │ │
│  └────────────┬───────────────┘ │
│               │                 │
│  ┌────────────▼───────────────┐ │
│  │  Processors                │ │
│  │  - Batch (reduce API calls)│ │
│  │  - Memory limiter          │ │
│  │  - Tail-based sampler      │ │
│  │  - Resource detection      │ │
│  │  - Attribute filter (PII)  │ │
│  └────────────┬───────────────┘ │
│               │                 │
│  ┌────────────▼───────────────┐ │
│  │  Exporters                 │ │
│  │  - Datadog (traces+metrics)│ │
│  │  - Prometheus remote write │ │
│  │  - Loki (logs)             │ │
│  │  - S3 (cold trace archive) │ │
│  └────────────────────────────┘ │
└─────────────────────────────────┘
```

---

## Key Design Decisions

### Tail-based sampling over head-based sampling

Head-based sampling (deciding at trace start whether to keep) was discarding precisely the traces we needed: slow requests and error paths. Tail-based sampling buffers the full trace in memory, evaluates the outcome, and keeps 100% of errors and p99+ latency traces while sampling successful fast traces at 10%.

This reduced Datadog APM ingest volume by ~40% while preserving full fidelity for debugging.

### PII scrubbing in the processor pipeline

Payment flows log request IDs, session tokens, and occasionally card metadata from upstream services. The attribute filter processor strips any span attribute matching a PII allowlist before export. This runs in the Collector, not in application code, so it cannot be accidentally bypassed by a service team.

### Kafka-backed buffer for spike resilience

During morning rush (7–9 AM PST), trace/metric volume spikes 8× baseline. A direct OTLP-to-backend pipeline dropped spans under load. Adding a Kafka topic as an intermediate buffer between edge Collectors and the Gateway Collector decoupled ingest from backend capacity, eliminating spike-driven data loss.

---

## Repository Structure

```
opentelemetry-pipeline/
├── collector/
│   ├── gateway-config.yaml          # Central gateway Collector configuration
│   ├── edge-agent-config.yaml       # Per-node agent Collector configuration
│   └── sampling-rules.yaml          # Tail-based sampling policy definitions
├── kubernetes/
│   ├── collector-daemonset.yaml     # Edge agent DaemonSet (one per node)
│   ├── gateway-deployment.yaml      # Gateway Collector Deployment
│   ├── gateway-hpa.yaml             # HorizontalPodAutoscaler for gateway
│   └── rbac.yaml                    # ServiceAccount and ClusterRole for Collector
├── helm/
│   └── values-production.yaml       # OpenTelemetry Operator Helm values
├── instrumentation/
│   ├── java-auto-instrumentation/   # Java agent config for Spring Boot services
│   ├── node-auto-instrumentation/   # Node.js auto-instrumentation setup
│   └── python-manual-examples/     # Manual span examples for Python services
├── terraform/
│   └── collector-infrastructure.tf  # Kafka buffer, IAM, and network resources
└── dashboards/
    └── pipeline-health.json         # Collector throughput and drop rate dashboard
```

---

## Sampling Strategy

| Trace Type | Sample Rate | Rationale |
|---|---|---|
| Errors (any status) | 100% | Full fidelity required for debugging |
| Latency > p99 threshold | 100% | Slow traces are the ones we investigate |
| Latency p95–p99 | 50% | Sufficient for statistical analysis |
| Successful, fast traces | 10% | Baseline visibility without cost explosion |
| Health check / liveness | 0% | No diagnostic value; noise reduction |

---

## Collector Resource Tuning

The memory limiter processor is critical for production stability. Without it, a downstream backend outage causes the Collector to buffer indefinitely and OOM.

```yaml
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 1500        # Hard limit; triggers forced GC
    spike_limit_mib: 400   # Headroom for spikes before rejecting new data
  batch:
    timeout: 5s
    send_batch_size: 1000
    send_batch_max_size: 2000
```

These values were tuned under production load. The Gateway runs on 2 vCPU / 4 GB nodes; the edge agent runs on 0.5 vCPU / 1 GB.

---

## Lessons Learned

**Start with the Collector before touching application SDKs.** The Collector can receive from multiple sources (Prometheus, Jaeger, Zipkin) during migration, so you can migrate backends without re-instrumenting services.

**The W3C `traceparent` header is a contract.** Any service in the trace chain that strips or replaces the header breaks distributed traces silently. Enforce header propagation through integration tests, not documentation.

**Kafka buffer depth needs to be sized to your longest backend outage, not your longest spike.** We initially sized for a 15-minute spike. A 2-hour backend maintenance window filled the buffer and we started dropping. Resize with outage duration in mind.

---

## Prerequisites

- Kubernetes 1.25+ (for OTel Operator)
- Helm 3.x
- OpenTelemetry Collector Contrib v0.90+
- Kafka 3.x (optional, for buffered pipeline)
- Datadog API key or compatible OTLP backend
