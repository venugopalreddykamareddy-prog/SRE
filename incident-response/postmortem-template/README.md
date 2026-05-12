# Postmortem Template and Process

A blameless postmortem framework for systematically learning from incidents and converting outage pain into engineering investment. Built from 9+ years of incident response experience and refined through hundreds of postmortems covering everything from 10-minute blips to 4-hour customer-impacting outages on a platform serving millions of daily active users.

---

## Philosophy: Blameless but Accountable

Blameless postmortems are frequently misunderstood. "Blameless" does not mean "consequence-free" or "nobody owns anything." It means:

- **We blame systems, not individuals.** If a human made a mistake, we ask: what system allowed or encouraged that mistake? Fix the system.
- **We assume good faith.** The engineer who ran the wrong kubectl command did so with the information they had at the time. They were not negligent; the runbook was ambiguous or the tooling was unsafe.
- **We hold systems accountable.** Every postmortem ends with action items that change something. A postmortem with no action items is just a story.

The practical result of this approach: engineers report incidents and near-misses accurately, without fear that honesty will damage their standing. Organizations that blame individuals for incidents get underreported incidents and engineers who cover their tracks. That is much more dangerous than the original incident.

---

## When to Write a Postmortem

A postmortem is required for:

- Any P1 incident (customer-facing, ordering flow degraded)
- Any P2 incident lasting more than 30 minutes
- Any incident that required emergency RBAC elevation
- Any incident that caused data inconsistency, even if corrected
- Any incident that came close to breaching an SLO (>50% error budget consumed in a week)

A postmortem is optional (encouraged) for:

- Near-misses caught by staging or canary
- Any incident where "we got lucky" was part of the recovery story
- Any manual intervention that could have been automated

---

## Postmortem Template

```markdown
# Postmortem: [Service/Feature] — [Failure Mode] — [YYYY-MM-DD]

## Severity & Duration
- **Severity:** P1 / P2
- **Start time:** YYYY-MM-DD HH:MM UTC
- **End time:** YYYY-MM-DD HH:MM UTC
- **Duration:** X hours Y minutes
- **Detection method:** [Alert / Customer report / On-call observation]
- **Detection lag:** [Time between incident start and first alert/detection]

## Customer & Business Impact
[What did customers experience? Be specific: "X% of order placement requests returned 503"
not "some users were affected." Include order volume, error count, or revenue impact if known.]

## Timeline

| Time (UTC) | Event |
|---|---|
| HH:MM | Incident begins (observable symptom or causative event) |
| HH:MM | Alert fires / On-call notified |
| HH:MM | On-call begins investigation |
| HH:MM | [Key diagnostic finding] |
| HH:MM | [Remediation action taken] |
| HH:MM | Service begins recovering |
| HH:MM | Full recovery confirmed |
| HH:MM | Incident closed in PagerDuty |

## Root Cause
[One or two clear sentences describing the technical root cause. This should be specific
enough that an engineer who was not involved can understand exactly what failed and why.]

## Contributing Factors
[List systemic factors that made this incident possible or made it worse. These are not
excuses — they are targets for follow-up engineering work.]

- [e.g., No alert existed for this failure mode]
- [e.g., Runbook for this service was 8 months out of date]
- [e.g., Circuit breaker timeout was set too high, prolonging cascade]

## What Went Well
[Honest acknowledgment of things that worked. This is as important as what went wrong.
What should we keep doing?]

- [e.g., On-call responded within 3 minutes]
- [e.g., Rollback procedure was well-documented and executed cleanly]
- [e.g., Customer communication went out within 10 minutes of impact confirmed]

## What Went Poorly
[Honest assessment of gaps. Focus on systems and processes, not individuals.]

- [e.g., Detection lag was 12 minutes due to missing SLO alert]
- [e.g., Runbook step 4 referenced a Datadog dashboard that had been renamed]
- [e.g., Escalation path was unclear; on-call pinged three people before reaching the right one]

## Action Items

| # | Action | Owner | Due Date | Priority |
|---|---|---|---|---|
| 1 | [Specific, testable action item] | @owner | YYYY-MM-DD | P1/P2/P3 |
| 2 | | | | |

**Action item rules:**
- Must be specific and testable (not "improve monitoring")
- Must have a single named owner
- Must have a due date
- Must close a contributing factor identified above

## Metrics
- **MTTR:** X min Y sec (detection to full recovery)
- **Detection lag:** X min (incident start to first alert)
- **Error budget consumed:** X.X% of monthly budget
- **Previous similar incidents:** [Link to past postmortem if this is a repeat]
```

---

## Repository Structure

```
postmortem-template/
├── template.md                    # The postmortem template (copy to use)
├── examples/
│   ├── rds-connection-exhaustion-2024-08.md   # Anonymized P1 example
│   ├── canary-rollback-failure-2024-11.md     # P1 with deployment context
│   └── redis-eviction-near-miss-2025-01.md   # Near-miss postmortem example
├── process/
│   ├── facilitation-guide.md      # How to run the postmortem meeting
│   ├── writing-guide.md           # Common mistakes in postmortem writing
│   └── action-item-tracking.md   # How to follow up on action items
└── metrics/
    └── postmortem-kpis.md         # How we measure postmortem program health
```

---

## Postmortem Meeting Facilitation

The postmortem meeting should occur within 5 business days of incident close. Key roles:

**Facilitator (SRE lead or senior IC):** Guides the timeline reconstruction, keeps discussion focused on systems not people, and ensures action items are specific and owned. Should not be the primary on-call engineer from the incident.

**Scribe:** Takes notes in real-time and drafts the action items during the meeting. The postmortem document should be 80% complete by the end of the meeting.

**Participants:** The on-call engineer(s), service owners of affected components, and anyone who contributed to the response. Optional: product/customer support for business impact context.

**Duration:** 60 minutes maximum. If the incident was complex, split into a 30-minute timeline reconstruction and a 30-minute action item session.

---

## Action Item Follow-Up

Action items without follow-up produce learned helplessness — engineers stop writing honest postmortems if they see that nothing changes. The follow-up process:

1. All action items are created as Jira tickets tagged `postmortem` immediately after the meeting
2. Action items are reviewed in the weekly SRE sync
3. Overdue action items (past due date, not completed) escalate to the SRE manager
4. Monthly: review what percentage of postmortem action items were completed on time

**Target:** 80% of action items completed within the stated due date.

---

## Measuring Postmortem Program Health

The postmortem program is healthy when:

- MTTR is trending downward month-over-month
- Repeat incidents (same root cause appearing in two postmortems) are rare and declining
- Detection lag is shrinking (better alerting from previous action items)
- Engineers are proactively writing postmortems for near-misses, not just P1/P2s
- Action item completion rate stays above 80%

A postmortem program that generates documents but doesn't change behavior is bureaucracy, not learning. Measure outcomes, not output.

---

## Resources

- Google SRE Book, Chapter 14: Managing Incidents
- Google SRE Workbook, Chapter 8: On-Call
- Etsy's Blameless Postmortems and a Just Culture (2012) — the foundational text
