# AI Log Anomaly Detector

A machine learning-based log anomaly detection system that identifies abnormal patterns in high-volume log streams before they surface as customer-impacting incidents. Built to address a recurring problem: traditional threshold-based alerts were either too noisy (constant false positives) or too slow (alerting only after the damage was done).

---

## Background

At Starbucks-scale log volume — approximately 2 million log lines per minute across the ordering platform — static keyword alerts and rate thresholds produce one of two failure modes:

- **Too sensitive**: Alert fatigue from hundreds of daily false positives. On-call engineers start ignoring alerts.
- **Too coarse**: Real incidents like a silent database connection pool exhaustion or a subtle memory leak go undetected for 20–30 minutes because they don't cross a hard threshold.

The AI anomaly detector was built to find the middle path: detect behavioral deviations from learned baselines without requiring a human to enumerate every failure mode in advance.

---

## How It Works

The system uses a two-stage detection approach:

### Stage 1 — Log Clustering and Vectorization

Raw log lines are noisy: they contain timestamps, request IDs, and dynamic values that make direct comparison meaningless. Stage 1 normalizes logs by:

1. Stripping dynamic tokens (UUIDs, IPs, timestamps, numeric values) using regex drain parsing
2. Clustering semantically similar log templates using the **Drain3** algorithm
3. Building a fixed-dimension feature vector from log template frequencies per time window (60-second buckets)

This converts a stream of 2M/min raw log lines into a structured time-series of ~800 features representing the behavioral fingerprint of the system.

### Stage 2 — Anomaly Scoring

Two complementary models run on the feature vectors:

- **Isolation Forest** — unsupervised; detects global statistical outliers. Good at catching sudden, dramatic shifts (e.g., a spike in database timeout log templates).
- **LSTM Autoencoder** — sequence model trained on 90 days of normal behavior. Detects subtle, slow-developing anomalies by measuring reconstruction error. Good at catching gradual memory leak patterns and slow degradation.

Scores from both models are fused into a single anomaly confidence score (0–1). A score above 0.75 triggers an alert.

---

## Architecture

```
Log Stream (Kafka topic: raw-logs)
        │
        ▼
┌─────────────────────────────────┐
│   Log Parser & Normalizer       │
│   (Drain3 template extraction)  │
└────────────┬────────────────────┘
             │ structured templates
             ▼
┌─────────────────────────────────┐
│   Feature Aggregator            │
│   (60s tumbling windows)        │
│   Output: feature vectors       │
└────────────┬────────────────────┘
             │
      ┌──────┴──────┐
      ▼             ▼
┌──────────┐  ┌──────────────┐
│ Isolation│  │ LSTM         │
│ Forest   │  │ Autoencoder  │
└──────────┘  └──────────────┘
      │             │
      └──────┬──────┘
             ▼
      Score Fusion
      (weighted average)
             │
      Anomaly Score > 0.75?
      ├── Yes → PagerDuty alert + Slack post with top contributing templates
      └── No  → Score logged to Datadog for trend monitoring
```

---

## Repository Structure

```
ai-log-anomaly-detector/
├── ingestion/
│   ├── kafka-consumer.py            # Kafka consumer for raw log stream
│   ├── drain-parser.py              # Drain3-based log template extraction
│   └── feature-aggregator.py       # 60s window feature vector builder
├── models/
│   ├── isolation-forest/
│   │   ├── train.py                 # Training pipeline
│   │   ├── score.py                 # Real-time scoring
│   │   └── model-config.yaml       # Contamination and n_estimators params
│   └── lstm-autoencoder/
│       ├── train.py                 # LSTM training with Keras
│       ├── score.py                 # Reconstruction error scoring
│       ├── architecture.py          # Model definition
│       └── model-config.yaml       # Window size, threshold, learning rate
├── fusion/
│   └── score-fusion.py             # Weighted model score combination
├── alerting/
│   ├── pagerduty-integration.py    # PagerDuty Events API v2 integration
│   └── slack-notifier.py           # Slack webhook with contributing templates
├── retraining/
│   ├── retrain-pipeline.yaml       # Weekly retraining Kubernetes CronJob
│   └── model-registry.py           # Model versioning and rollback
├── evaluation/
│   ├── precision-recall-eval.py    # Alert quality measurement
│   └── labeled-incidents/          # Ground truth labels from postmortems
└── kubernetes/
    ├── detector-deployment.yaml    # Detector Deployment spec
    └── retraining-cronjob.yaml     # Weekly model retraining job
```

---

## Model Performance

After 6 months in production, evaluated against labeled incidents from postmortems:

| Metric | Isolation Forest | LSTM Autoencoder | Fused |
|---|---|---|---|
| Precision | 71% | 68% | **84%** |
| Recall | 89% | 94% | **91%** |
| False positive rate | 0.8/day | 1.2/day | **0.4/day** |
| Median detection lead time | 4.2 min | 7.8 min | **6.1 min** |

The fused model significantly reduces false positives while preserving recall — the most important operational property (missing a real incident is worse than a false alarm).

---

## Retraining Strategy

Models are retrained weekly using the previous 90 days of labeled data. The labeling pipeline:

1. All confirmed incidents (from PagerDuty and postmortems) are marked as anomalies in the training set
2. Human-confirmed false positives are used to tune the fusion threshold
3. A/B comparison runs for 48 hours before the new model fully replaces the old one

**Model rollback** is one command if a new model degrades precision in production.

---

## Alert Format

When an anomaly is detected, the Slack notification includes:

```
[ANOMALY DETECTED] Service: ordering-api | Score: 0.87
Top contributing log templates (last 60s):
  +340% — "DB connection pool exhausted after Xms"
  +180% — "Retry attempt N for downstream service Y"
  +120% — "Circuit breaker OPEN on payment-service"

Isolation Forest: 0.82 | LSTM Autoencoder: 0.94
→ PagerDuty incident created: PD-XXXXX
→ Runbook: <link>
```

The contributing templates give the on-call engineer a head start before they even open the monitoring dashboard.

---

## Lessons Learned

**Template extraction quality determines everything.** Drain3 with default settings produced 4,000+ templates from noisy logs. Tuning the `sim_threshold` and adding custom pre-processing rules reduced it to ~800 stable templates, which dramatically improved model signal quality.

**Score thresholds need periodic recalibration.** As the system evolves (new services, deployments, traffic pattern shifts), the baseline drifts. The weekly retraining handles model drift, but the fusion threshold needs quarterly review against recent false positive/negative rates.

**Explain the alert or it gets ignored.** The first version sent only a score and a severity. On-call engineers dismissed them without the context of which log templates drove the score. Adding contributing templates to every alert reduced "acknowledged without investigation" rates by 60%.

---

## Prerequisites

- Python 3.10+
- Kafka 3.x (log stream source)
- Kubernetes (for deployment and retraining CronJob)
- scikit-learn >= 1.3 (Isolation Forest)
- TensorFlow 2.x / Keras (LSTM Autoencoder)
- Drain3 library
- PagerDuty Events API v2 key
