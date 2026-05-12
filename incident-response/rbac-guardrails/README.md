# RBAC Guardrails

Role-Based Access Control policies and enforcement frameworks designed to limit blast radius during infrastructure incidents. This work was built directly in response to an Azure outage where overly permissive IAM assignments allowed an automated recovery script to escalate its own privileges and make unintended infrastructure changes — turning a P2 into a P1.

---

## Background: The Incident That Built This

In Q3, a runbook automation script triggered during a Redis failover used a service principal that had been granted `Contributor` role on the resource group for "convenience during the migration." The script, responding to what it interpreted as a degraded state, began deprovisioning and re-provisioning resources it was never meant to touch. The actual Redis failover took 4 minutes. Cleaning up the unintended side effects took 3 hours.

The postmortem produced a single root-cause finding: **the principle of least privilege had been acknowledged in policy but not enforced in practice.** Service principals, CI/CD pipelines, and human operator roles had accumulated permissions over time with no systematic review.

This RBAC guardrails project is the direct output of that postmortem's action items.

---

## Principles

### Least privilege by default, elevation by exception

No service account, pipeline identity, or human role should hold permissions beyond what its narrowest legitimate operation requires. When broader access is needed temporarily, it is granted via a time-bound elevation request with audit logging — not by editing the base role assignment.

### Role assignments are code, reviewed like code

All IAM/RBAC assignments are declared in Terraform and merged through pull requests with a required SRE review. Ad-hoc changes via the console are detected by a daily drift scanner and trigger an alert. "It was faster to click through the portal" is not a valid reason to bypass the PR process.

### Separate what can read from what can write from what can delete

Read, write, and delete permissions are distinct roles at every layer: Kubernetes RBAC, Azure IAM, AWS IAM, GCP IAM. A service that reads from a database should not have the `DROP TABLE` permission. This seems obvious but is violated constantly in practice.

### Automated remediation accounts have the tightest scope of all

The worst blast-radius scenarios involve automation, not humans. Humans hesitate. Automation executes at machine speed. A runbook automation principal has the narrowest possible scope: exactly the resources and operations it needs to execute its defined remediation, nothing more.

---

## Repository Structure

```
rbac-guardrails/
├── kubernetes/
│   ├── namespaced-roles.yaml         # Per-namespace Role definitions
│   ├── cluster-roles.yaml            # ClusterRole definitions (minimal set)
│   ├── service-account-bindings.yaml # RoleBinding per service account
│   └── audit-policy.yaml            # K8s API audit logging policy
├── azure/
│   ├── custom-roles/
│   │   ├── sre-operator-role.json    # SRE human operator: read + specific writes
│   │   ├── automation-role.json      # CI/CD pipeline: deploy only, no delete
│   │   └── runbook-role.json         # Runbook automation: per-resource, tightly scoped
│   ├── terraform/
│   │   ├── role-assignments.tf       # All Azure RBAC assignments as code
│   │   └── pim-policies.tf           # PIM just-in-time elevation policies
│   └── drift-detection/
│       └── rbac-drift-scanner.py     # Daily drift detection vs. Terraform state
├── aws/
│   ├── iam-policies/
│   │   ├── sre-operator-policy.json  # SRE human operator policy
│   │   ├── eks-node-policy.json      # EKS node group IAM policy (minimal)
│   │   └── ci-deploy-policy.json     # CI/CD pipeline IAM policy (deploy only)
│   └── terraform/
│       └── iam-resources.tf          # AWS IAM users, roles, policies as code
├── gcp/
│   ├── iam-bindings.yaml             # GCP IAM bindings (Terraform-managed)
│   └── workload-identity/
│       └── wi-bindings.tf            # Workload Identity Federation for GKE
├── reviews/
│   ├── quarterly-access-review.md   # Template for quarterly privilege review
│   └── review-checklist.md          # Line-by-line review checklist
└── runbooks/
    └── emergency-elevation.md        # How to request emergency elevated access
```

---

## Role Definitions Reference

### Kubernetes Roles

| Role | Namespace Scope | Permissions |
|---|---|---|
| `sre-read-only` | All namespaces | get, list, watch on all resources |
| `sre-operator` | Production | + exec, port-forward, delete pods |
| `service-deployer` | Service namespace | get/list/watch deployments; update deployments only |
| `runbook-automation` | Specific namespace | patch deployments; delete specific named resources |
| `cluster-admin` | Cluster | All (requires PIM elevation, max 4h) |

### Azure Custom Roles

**`sre-operator`** — Human SRE on-call role:
- Can read all resources
- Can restart App Service, AKS node pool, Redis
- Can NOT delete resource groups, modify IAM assignments, or access key vaults directly

**`automation-principal`** — CI/CD pipeline identity:
- Can deploy to specific AKS namespaces
- Can update App Service configurations
- Can NOT create or delete resource groups, modify network security groups, or assign roles

**`runbook-principal`** — Automated remediation identity:
- Scoped to specific resource group only
- Can failover Redis, restart specific App Services
- Can NOT deprovision, create new resources, or modify IAM

---

## Drift Detection

The drift scanner runs daily via Kubernetes CronJob. It:

1. Pulls current IAM assignments from the Azure/AWS/GCP APIs
2. Compares against the Terraform state file
3. Reports any assignments present in cloud but absent from Terraform (drift)
4. Reports any assignments in Terraform but absent from cloud (deletion)

Drifted assignments trigger a PagerDuty P3 and a Slack message with the offending assignment. The expectation is resolution within 24 hours: either the assignment is removed, or a PR is opened to codify the legitimate change.

Since deployment, the drift scanner has caught 14 undeclared role assignments — most were leftover from debug sessions that were never cleaned up.

---

## Quarterly Access Review Process

Every quarter, a structured access review is conducted:

1. Export all active role assignments across all three clouds
2. For each assignment, answer three questions:
   - Does this identity still exist?
   - Does this role still match the minimum permissions this identity requires?
   - When was this assignment last used (using cloud provider access logs)?
3. Any assignment unused for 90 days is scheduled for removal
4. Any human assignment with write/delete permissions requires re-justification

The quarterly review has removed an average of 23% of existing role assignments each cycle. Permissions accumulate without active pruning.

---

## Emergency Elevation Procedure

When an incident requires permissions beyond what the on-call engineer's base role provides:

1. Open a PIM just-in-time request in Azure / AWS IAM temporary role assumption
2. Specify the required permission scope and estimated duration (max 4 hours)
3. An SRE lead approves or auto-approves for P1 incidents
4. All actions taken under elevated access are audit-logged to a dedicated Splunk index
5. Elevation expires automatically; no manual cleanup required

**Do not grant permanent elevated access to resolve an incident.** The pressure to "fix it and clean it up later" is how overly permissive assignments persist forever.

---

## Lessons Learned from the Azure Outage

1. **Service principals accumulate permissions like entropy.** Every migration, every "temporary" fix, every shortcut adds permissions. Remove them explicitly and immediately after use.
2. **Automation needs narrower permissions than humans, not broader ones.** Humans hesitate and question. Automation acts on every permission it holds.
3. **`Contributor` on a resource group is never the right scope for automation.** Always scope to the specific resource type and specific resource name.
4. **Audit logs are useless if nobody reads them.** The overly permissive assignment was visible in Azure Activity Log for months. We weren't looking. Now the drift scanner looks for us.
