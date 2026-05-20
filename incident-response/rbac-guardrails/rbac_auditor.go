// rbac_auditor validates Kubernetes RBAC configuration against a declared
// policy baseline and reports violations. Designed to run as a daily CronJob
// or as a pre-merge CI check on changes to RBAC manifests.
//
// Usage:
//
//	rbac_auditor --policy policy.yaml --namespace production --output report.json
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/yaml"
)

// ---------------------------------------------------------------------------
// Policy config
// ---------------------------------------------------------------------------

type AllowedVerbs []string

type RolePolicy struct {
	Name         string       `yaml:"name"`
	AllowedVerbs AllowedVerbs `yaml:"allowed_verbs"`
	// Resources this role may act on; empty means any is a violation.
	AllowedResources []string `yaml:"allowed_resources"`
	// MaxNamespaceScope: "namespaced" | "cluster" — cluster-wide roles are
	// only permitted for a small set of infrastructure accounts.
	MaxNamespaceScope string `yaml:"max_namespace_scope"`
}

type PolicyConfig struct {
	// Roles that are explicitly permitted to bind to service accounts in production.
	AllowedRoles []RolePolicy `yaml:"allowed_roles"`
	// Service accounts that may hold cluster-admin equivalent bindings.
	PrivilegedAccounts []string `yaml:"privileged_accounts"`
	// Verbs that are never permitted on any production service account.
	ForbiddenVerbs []string `yaml:"forbidden_verbs"`
	// Resources that require an explicit exception to access.
	SensitiveResources []string `yaml:"sensitive_resources"`
}

func loadPolicy(path string) (*PolicyConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read policy file: %w", err)
	}
	var cfg PolicyConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse policy: %w", err)
	}
	return &cfg, nil
}

// ---------------------------------------------------------------------------
// Violation types
// ---------------------------------------------------------------------------

type SeverityLevel string

const (
	Critical SeverityLevel = "CRITICAL"
	High     SeverityLevel = "HIGH"
	Medium   SeverityLevel = "MEDIUM"
	Low      SeverityLevel = "LOW"
)

type Violation struct {
	Severity    SeverityLevel `json:"severity"`
	Resource    string        `json:"resource"`
	Namespace   string        `json:"namespace,omitempty"`
	Subject     string        `json:"subject"`
	Description string        `json:"description"`
	Remediation string        `json:"remediation"`
}

type AuditReport struct {
	GeneratedAt   time.Time   `json:"generated_at"`
	Cluster       string      `json:"cluster"`
	Namespace     string      `json:"namespace"`
	PolicyFile    string      `json:"policy_file"`
	Violations    []Violation `json:"violations"`
	TotalChecked  int         `json:"total_checked"`
	PassCount     int         `json:"pass_count"`
	ViolatorCount int         `json:"violator_count"`
}

// ---------------------------------------------------------------------------
// Kubernetes client
// ---------------------------------------------------------------------------

func newKubeClient(kubeconfig string) (*kubernetes.Clientset, string, error) {
	rules := clientcmd.NewDefaultClientConfigLoadingRules()
	if kubeconfig != "" {
		rules.ExplicitPath = kubeconfig
	}
	cfg, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		rules, &clientcmd.ConfigOverrides{},
	).ClientConfig()
	if err != nil {
		return nil, "", fmt.Errorf("build kubeconfig: %w", err)
	}
	cs, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, "", fmt.Errorf("create client: %w", err)
	}
	// Surface the cluster server address for the report header.
	return cs, cfg.Host, nil
}

// ---------------------------------------------------------------------------
// Auditors
// ---------------------------------------------------------------------------

type Auditor struct {
	cs     *kubernetes.Clientset
	policy *PolicyConfig
	ns     string
}

func (a *Auditor) auditClusterRoleBindings(ctx context.Context) ([]Violation, int, error) {
	list, err := a.cs.RbacV1().ClusterRoleBindings().List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, 0, fmt.Errorf("list ClusterRoleBindings: %w", err)
	}

	var violations []Violation
	checked := 0

	for _, crb := range list.Items {
		for _, subject := range crb.Subjects {
			if subject.Kind != "ServiceAccount" {
				continue
			}
			checked++
			fullName := fmt.Sprintf("%s/%s", subject.Namespace, subject.Name)

			// Cluster-admin equivalent bindings are only permitted for a declared set.
			if isClusterAdmin(crb.RoleRef.Name) && !a.isPrivileged(fullName) {
				violations = append(violations, Violation{
					Severity:    Critical,
					Resource:    "ClusterRoleBinding/" + crb.Name,
					Subject:     fullName,
					Description: fmt.Sprintf("ServiceAccount bound to %q (cluster-admin equivalent)", crb.RoleRef.Name),
					Remediation: "Remove binding or add account to privileged_accounts policy with justification.",
				})
			}

			// Any service account with a ClusterRoleBinding needs justification
			// unless it's in the privileged list.
			if !isClusterAdmin(crb.RoleRef.Name) && !a.isPrivileged(fullName) {
				violations = append(violations, Violation{
					Severity:    High,
					Resource:    "ClusterRoleBinding/" + crb.Name,
					Subject:     fullName,
					Description: "ServiceAccount has cluster-scoped binding; prefer namespace-scoped RoleBinding",
					Remediation: "Replace with a namespaced RoleBinding scoped to the service's namespace.",
				})
			}
		}
	}
	return violations, checked, nil
}

func (a *Auditor) auditRoleBindings(ctx context.Context) ([]Violation, int, error) {
	list, err := a.cs.RbacV1().RoleBindings(a.ns).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, 0, fmt.Errorf("list RoleBindings in %s: %w", a.ns, err)
	}

	var violations []Violation
	checked := 0

	for _, rb := range list.Items {
		for _, subject := range rb.Subjects {
			if subject.Kind != "ServiceAccount" {
				continue
			}
			checked++
			a.checkForbiddenVerbs(ctx, rb.RoleRef.Name, rb.Namespace, subject.Name, &violations)
			a.checkSensitiveResources(ctx, rb.RoleRef.Name, rb.Namespace, subject.Name, &violations)
		}
	}
	return violations, checked, nil
}

func (a *Auditor) checkForbiddenVerbs(
	ctx context.Context, roleName, ns, subject string, violations *[]Violation,
) {
	rules := a.fetchRoleRules(ctx, roleName, ns)
	for _, rule := range rules {
		for _, verb := range rule.Verbs {
			if a.isForbiddenVerb(verb) {
				*violations = append(*violations, Violation{
					Severity:    Critical,
					Resource:    fmt.Sprintf("Role/%s", roleName),
					Namespace:   ns,
					Subject:     fmt.Sprintf("%s/%s", ns, subject),
					Description: fmt.Sprintf("Forbidden verb %q granted on %v", verb, rule.Resources),
					Remediation: fmt.Sprintf("Remove verb %q from role %q or narrow to a non-sensitive resource.", verb, roleName),
				})
			}
		}
	}
}

func (a *Auditor) checkSensitiveResources(
	ctx context.Context, roleName, ns, subject string, violations *[]Violation,
) {
	rules := a.fetchRoleRules(ctx, roleName, ns)
	for _, rule := range rules {
		for _, resource := range rule.Resources {
			if a.isSensitiveResource(resource) {
				*violations = append(*violations, Violation{
					Severity:    High,
					Resource:    fmt.Sprintf("Role/%s", roleName),
					Namespace:   ns,
					Subject:     fmt.Sprintf("%s/%s", ns, subject),
					Description: fmt.Sprintf("Access to sensitive resource %q with verbs %v", resource, rule.Verbs),
					Remediation: "Scope to a specific resource name or remove access if not required.",
				})
			}
		}
	}
}

func (a *Auditor) fetchRoleRules(ctx context.Context, roleName, ns string) []rbacv1.PolicyRule {
	role, err := a.cs.RbacV1().Roles(ns).Get(ctx, roleName, metav1.GetOptions{})
	if err != nil {
		// TODO: log the error instead of silently returning nil — missing roles skew the audit results
		return nil
	}
	return role.Rules
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func isClusterAdmin(roleName string) bool {
	// TODO: move this list into the policy config so it doesn't require a code change to update
	privileged := []string{"cluster-admin", "admin", "edit"}
	for _, p := range privileged {
		if roleName == p {
			return true
		}
	}
	return false
}

func (a *Auditor) isPrivileged(fullName string) bool {
	for _, pa := range a.policy.PrivilegedAccounts {
		if pa == fullName {
			return true
		}
	}
	return false
}

func (a *Auditor) isForbiddenVerb(verb string) bool {
	for _, fv := range a.policy.ForbiddenVerbs {
		if strings.EqualFold(fv, verb) || fv == "*" {
			return true
		}
	}
	return false
}

func (a *Auditor) isSensitiveResource(resource string) bool {
	for _, sr := range a.policy.SensitiveResources {
		if strings.EqualFold(sr, resource) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Report output
// ---------------------------------------------------------------------------

func printSummary(report AuditReport) {
	fmt.Printf("\nRBAC Audit — %s\n", report.Cluster)
	fmt.Printf("Namespace:   %s\n", report.Namespace)
	fmt.Printf("Checked:     %d bindings\n", report.TotalChecked)
	fmt.Printf("Violations:  %d\n", report.ViolatorCount)
	fmt.Println(strings.Repeat("─", 60))

	for _, v := range report.Violations {
		fmt.Printf("[%s] %s\n  Subject: %s\n  Issue: %s\n  Fix: %s\n\n",
			v.Severity, v.Resource, v.Subject, v.Description, v.Remediation)
	}
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	policyFile := flag.String("policy", "kubernetes/rbac-policy.yaml", "Path to policy YAML")
	namespace := flag.String("namespace", "production", "Namespace to audit (use \"\" for all)")
	outputFile := flag.String("output", "", "Write JSON report to this path")
	kubeconfig := flag.String("kubeconfig", "", "Path to kubeconfig (defaults to in-cluster config)")
	flag.Parse()

	policy, err := loadPolicy(*policyFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(2)
	}

	cs, clusterHost, err := newKubeClient(*kubeconfig)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(2)
	}

	auditor := &Auditor{cs: cs, policy: policy, ns: *namespace}
	ctx := context.Background()

	var allViolations []Violation
	totalChecked := 0

	crbViolations, crbChecked, err := auditor.auditClusterRoleBindings(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "WARN: %v\n", err)
	}
	allViolations = append(allViolations, crbViolations...)
	totalChecked += crbChecked

	rbViolations, rbChecked, err := auditor.auditRoleBindings(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "WARN: %v\n", err)
	}
	allViolations = append(allViolations, rbViolations...)
	totalChecked += rbChecked

	report := AuditReport{
		GeneratedAt:   time.Now().UTC(),
		Cluster:       clusterHost,
		Namespace:     *namespace,
		PolicyFile:    *policyFile,
		Violations:    allViolations,
		TotalChecked:  totalChecked,
		ViolatorCount: len(allViolations),
		PassCount:     totalChecked - len(allViolations),
	}

	printSummary(report)

	if *outputFile != "" {
		data, _ := json.MarshalIndent(report, "", "  ")
		if err := os.WriteFile(*outputFile, data, 0644); err != nil {
			fmt.Fprintf(os.Stderr, "ERROR writing report: %v\n", err)
			os.Exit(2)
		}
		fmt.Printf("Report written to %s\n", *outputFile)
	}

	if len(allViolations) > 0 {
		os.Exit(1)
	}
}
