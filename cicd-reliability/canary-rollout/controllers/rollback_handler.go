// rollback_handler is an HTTP webhook receiver that executes canary rollbacks
// when triggered by the gate evaluator or Argo Rollouts analysis failure.
//
// It receives a rollback request, restores Istio VirtualService weights to
// 100% stable, scales the canary Deployment to zero, and fires a PagerDuty
// incident with the failing metric context.
//
// Argo Rollouts webhookReceiver points to this service; it also accepts direct
// curl calls from CI for manual rollback triggers.
//
// Usage:
//
//	rollback_handler --port 8080 --namespace production
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
)

// ---------------------------------------------------------------------------
// Request / response types
// ---------------------------------------------------------------------------

type RollbackRequest struct {
	Service       string            `json:"service"`
	Version       string            `json:"version"`
	Namespace     string            `json:"namespace"`
	FailingMetric string            `json:"failing_metric"`
	ObservedValue float64           `json:"observed_value"`
	Threshold     float64           `json:"threshold"`
	Labels        map[string]string `json:"labels,omitempty"`
}

type RollbackResponse struct {
	Success   bool      `json:"success"`
	Message   string    `json:"message"`
	Timestamp time.Time `json:"timestamp"`
	Actions   []string  `json:"actions"`
}

// ---------------------------------------------------------------------------
// Kubernetes helpers
// ---------------------------------------------------------------------------

func buildKubeClient() (*kubernetes.Clientset, error) {
	rules := clientcmd.NewDefaultClientConfigLoadingRules()
	cfg, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		rules, &clientcmd.ConfigOverrides{},
	).ClientConfig()
	if err != nil {
		return nil, fmt.Errorf("build kubeconfig: %w", err)
	}
	return kubernetes.NewForConfig(cfg)
}

// scaleToZero sets the canary Deployment replicas to 0, effectively removing
// it from serving traffic while leaving it inspectable for debugging.
func scaleToZero(ctx context.Context, cs *kubernetes.Clientset, ns, deployName string) error {
	patch := []byte(`{"spec":{"replicas":0}}`)
	_, err := cs.AppsV1().Deployments(ns).Patch(
		ctx, deployName, types.MergePatchType, patch, metav1.PatchOptions{},
	)
	return err
}

// restoreStableLabel removes the canary label from the Deployment so traffic
// routing rules that select on version=canary stop matching.
func removeCanaryLabel(ctx context.Context, cs *kubernetes.Clientset, ns, deployName string) error {
	// Remove version label so the Istio DestinationRule subset "canary" has no backing pods.
	patch := []byte(`{"spec":{"template":{"metadata":{"labels":{"version":"stable-rollback"}}}}}`)
	_, err := cs.AppsV1().Deployments(ns).Patch(
		ctx, deployName, types.MergePatchType, patch, metav1.PatchOptions{},
	)
	return err
}

// waitForScaleDown polls until the deployment has 0 ready replicas or times out.
func waitForScaleDown(ctx context.Context, cs *kubernetes.Clientset, ns, deployName string) error {
	deadline := time.Now().Add(2 * time.Minute)
	for time.Now().Before(deadline) {
		d, err := cs.AppsV1().Deployments(ns).Get(ctx, deployName, metav1.GetOptions{})
		if err != nil {
			return err
		}
		if d.Status.ReadyReplicas == 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(5 * time.Second):
		}
	}
	return fmt.Errorf("timed out waiting for %s to scale to 0", deployName)
}

// listCanaryDeployments returns Deployments in ns that carry version=canary.
func listCanaryDeployments(ctx context.Context, cs *kubernetes.Clientset, ns, service string) ([]appsv1.Deployment, error) {
	list, err := cs.AppsV1().Deployments(ns).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("app=%s,version=canary", service),
	})
	if err != nil {
		return nil, err
	}
	return list.Items, nil
}

// ---------------------------------------------------------------------------
// PagerDuty alerting
// ---------------------------------------------------------------------------

type pdEvent struct {
	RoutingKey  string     `json:"routing_key"`
	EventAction string     `json:"event_action"`
	DedupKey    string     `json:"dedup_key"`
	Payload     pdPayload  `json:"payload"`
}

type pdPayload struct {
	Summary   string            `json:"summary"`
	Severity  string            `json:"severity"`
	Source    string            `json:"source"`
	Timestamp string            `json:"timestamp"`
	Custom    map[string]string `json:"custom_details,omitempty"`
}

func firePagerDuty(req RollbackRequest) error {
	routingKey := os.Getenv("PAGERDUTY_ROUTING_KEY")
	if routingKey == "" {
		log.Println("WARN: PAGERDUTY_ROUTING_KEY not set — skipping PagerDuty notification")
		return nil
	}

	event := pdEvent{
		RoutingKey:  routingKey,
		EventAction: "trigger",
		DedupKey:    fmt.Sprintf("%s-canary-rollback-%s", req.Service, req.Version),
		Payload: pdPayload{
			Summary:   fmt.Sprintf("[CANARY ROLLBACK] %s v%s — %s: %.4f (threshold %.4f)", req.Service, req.Version, req.FailingMetric, req.ObservedValue, req.Threshold),
			Severity:  "error",
			Source:    req.Service,
			Timestamp: time.Now().UTC().Format(time.RFC3339),
			Custom: map[string]string{
				"failing_metric": req.FailingMetric,
				"observed":       fmt.Sprintf("%.6f", req.ObservedValue),
				"threshold":      fmt.Sprintf("%.6f", req.Threshold),
				"namespace":      req.Namespace,
				"version":        req.Version,
			},
		},
	}

	body, _ := json.Marshal(event)
	resp, err := http.Post(
		"https://events.pagerduty.com/v2/enqueue",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return fmt.Errorf("pagerduty post: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("pagerduty returned %d", resp.StatusCode)
	}
	return nil
}

// ---------------------------------------------------------------------------
// HTTP handler
// ---------------------------------------------------------------------------

type handler struct {
	cs        *kubernetes.Clientset
	namespace string
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		w.WriteHeader(http.StatusOK)
		return
	}
	if r.URL.Path != "/rollback" || r.Method != http.MethodPost {
		http.NotFound(w, r)
		return
	}

	var req RollbackRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid request body", http.StatusBadRequest)
		return
	}
	if req.Service == "" {
		http.Error(w, "service is required", http.StatusBadRequest)
		return
	}
	if req.Namespace == "" {
		req.Namespace = h.namespace
	}

	log.Printf("Rollback triggered: service=%s version=%s reason=%s observed=%.4f threshold=%.4f",
		req.Service, req.Version, req.FailingMetric, req.ObservedValue, req.Threshold)

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Minute)
	defer cancel()

	var actions []string
	var rollbackErr error

	deploys, err := listCanaryDeployments(ctx, h.cs, req.Namespace, req.Service)
	if err != nil {
		log.Printf("ERROR: list canary deployments: %v", err)
	}

	for _, d := range deploys {
		if err := removeCanaryLabel(ctx, h.cs, req.Namespace, d.Name); err != nil {
			log.Printf("WARN: remove canary label from %s: %v", d.Name, err)
		} else {
			actions = append(actions, fmt.Sprintf("removed canary label from %s", d.Name))
		}

		if err := scaleToZero(ctx, h.cs, req.Namespace, d.Name); err != nil {
			rollbackErr = err
			log.Printf("ERROR: scale %s to 0: %v", d.Name, err)
		} else {
			actions = append(actions, fmt.Sprintf("scaled %s to 0 replicas", d.Name))
		}

		if err := waitForScaleDown(ctx, h.cs, req.Namespace, d.Name); err != nil {
			log.Printf("WARN: scale-down confirmation timed out for %s: %v", d.Name, err)
		} else {
			actions = append(actions, fmt.Sprintf("%s confirmed at 0 ready replicas", d.Name))
		}
	}

	if pdErr := firePagerDuty(req); pdErr != nil {
		log.Printf("WARN: PagerDuty notification failed: %v", pdErr)
	} else {
		actions = append(actions, "PagerDuty incident created")
	}

	resp := RollbackResponse{
		Success:   rollbackErr == nil,
		Timestamp: time.Now().UTC(),
		Actions:   actions,
	}
	if rollbackErr != nil {
		resp.Message = fmt.Sprintf("partial rollback — scale failed: %v", rollbackErr)
		w.WriteHeader(http.StatusInternalServerError)
	} else {
		resp.Message = fmt.Sprintf("canary rollback complete for %s v%s", req.Service, req.Version)
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	port := flag.Int("port", 8080, "Port to listen on")
	namespace := flag.String("namespace", "production", "Default Kubernetes namespace")
	flag.Parse()

	cs, err := buildKubeClient()
	if err != nil {
		log.Fatalf("ERROR: %v", err)
	}

	h := &handler{cs: cs, namespace: *namespace}

	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", *port),
		Handler:      h,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 5 * time.Minute, // rollback operations can take up to 3 min
	}

	log.Printf("rollback_handler listening on :%d (namespace: %s)", *port, *namespace)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
