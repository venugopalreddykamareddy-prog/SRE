// slo_exporter exposes SLO burn rate and error budget metrics as a Prometheus
// /metrics endpoint. Runs as a sidecar or standalone Deployment; Prometheus
// scrapes it on the standard interval and Grafana reads the resulting series.
//
// Metrics exposed:
//
//	slo_error_budget_remaining_ratio{service, slo_name}
//	slo_burn_rate{service, slo_name, window}   — windows: 1h, 6h, 72h
//	slo_compliance_ratio{service, slo_name}    — current SLI vs. target
//
// Usage:
//
//	slo_exporter --config slo-config.yaml --port 9090
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"sigs.k8s.io/yaml"
)

// ---------------------------------------------------------------------------
// SLO config
// ---------------------------------------------------------------------------

type SLODefinition struct {
	Name              string  `yaml:"name"`
	Service           string  `yaml:"service"`
	Target            float64 `yaml:"target"`             // e.g. 0.999
	ErrorsMetric      string  `yaml:"errors_metric"`      // Datadog metric query
	TotalMetric       string  `yaml:"total_metric"`       // Datadog metric query
	WindowDays        int     `yaml:"window_days"`        // rolling window for SLI (default 30)
}

type ExporterConfig struct {
	SLOs             []SLODefinition `yaml:"slos"`
	DatadogSite      string          `yaml:"datadog_site"` // e.g. datadoghq.com
	ScrapeIntervalS  int             `yaml:"scrape_interval_seconds"`
}

func loadConfig(path string) (*ExporterConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	var cfg ExporterConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	if cfg.ScrapeIntervalS == 0 {
		cfg.ScrapeIntervalS = 60
	}
	if cfg.DatadogSite == "" {
		cfg.DatadogSite = "datadoghq.com"
	}
	return &cfg, nil
}

// ---------------------------------------------------------------------------
// Datadog query client
// ---------------------------------------------------------------------------

type ddPoint struct {
	Timestamp int64
	Value     float64
}

type ddClient struct {
	apiKey string
	appKey string
	site   string
	http   *http.Client
}

func newDDClient(site string) *ddClient {
	return &ddClient{
		apiKey: os.Getenv("DD_API_KEY"),
		appKey: os.Getenv("DD_APP_KEY"),
		site:   site,
		http:   &http.Client{Timeout: 15 * time.Second},
	}
}

func (c *ddClient) queryAvg(ctx context.Context, query string, from, to int64) (float64, error) {
	url := fmt.Sprintf(
		"https://api.%s/api/v1/query?query=%s&from=%d&to=%d",
		c.site, query, from, to,
	)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	req.Header.Set("DD-API-KEY", c.apiKey)
	req.Header.Set("DD-APPLICATION-KEY", c.appKey)

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("datadog query: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("datadog returned %d", resp.StatusCode)
	}

	var payload struct {
		Series []struct {
			Pointlist [][2]*float64 `json:"pointlist"`
		} `json:"series"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return 0, fmt.Errorf("decode response: %w", err)
	}
	if len(payload.Series) == 0 || len(payload.Series[0].Pointlist) == 0 {
		return 0, nil
	}

	var sum float64
	var count int
	for _, pt := range payload.Series[0].Pointlist {
		if pt[1] != nil {
			sum += *pt[1]
			count++
		}
	}
	if count == 0 {
		return 0, nil
	}
	return sum / float64(count), nil
}

func (c *ddClient) errorRate(ctx context.Context, errQ, totalQ string, windowSeconds int64) (float64, error) {
	now := time.Now().Unix()
	from := now - windowSeconds

	errors, err := c.queryAvg(ctx, errQ, from, now)
	if err != nil {
		return 0, err
	}
	total, err := c.queryAvg(ctx, totalQ, from, now)
	if err != nil {
		return 0, err
	}
	if total == 0 {
		return 0, nil
	}
	return errors / total, nil
}

// ---------------------------------------------------------------------------
// Prometheus collector
// ---------------------------------------------------------------------------

var burnWindows = []struct {
	label   string
	seconds int64
}{
	{"1h", 3_600},
	{"6h", 21_600},
	{"72h", 259_200},
}

type SLOCollector struct {
	cfg *ExporterConfig
	dd  *ddClient
	mu  sync.Mutex

	descBudgetRemaining *prometheus.Desc
	descBurnRate        *prometheus.Desc
	descCompliance      *prometheus.Desc
}

func NewSLOCollector(cfg *ExporterConfig, dd *ddClient) *SLOCollector {
	labels := []string{"service", "slo_name"}
	burnLabels := []string{"service", "slo_name", "window"}
	return &SLOCollector{
		cfg: cfg,
		dd:  dd,
		descBudgetRemaining: prometheus.NewDesc(
			"slo_error_budget_remaining_ratio",
			"Fraction of monthly error budget remaining (1.0 = full budget, 0.0 = exhausted)",
			labels, nil,
		),
		descBurnRate: prometheus.NewDesc(
			"slo_burn_rate",
			"Current error budget burn rate relative to sustainable rate (1.0 = sustainable, 14.4 = fast burn)",
			burnLabels, nil,
		),
		descCompliance: prometheus.NewDesc(
			"slo_compliance_ratio",
			"Current SLI value as a ratio (1.0 = perfect, target e.g. 0.999 for 99.9% SLO)",
			labels, nil,
		),
	}
}

func (c *SLOCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.descBudgetRemaining
	ch <- c.descBurnRate
	ch <- c.descCompliance
}

func (c *SLOCollector) Collect(ch chan<- prometheus.Metric) {
	c.mu.Lock()
	defer c.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	for _, slo := range c.cfg.SLOs {
		windowDays := slo.WindowDays
		if windowDays == 0 {
			windowDays = 30
		}
		windowSeconds := int64(windowDays * 24 * 3600)

		// Rolling SLI for compliance metric.
		errRate, err := c.dd.errorRate(ctx, slo.ErrorsMetric, slo.TotalMetric, windowSeconds)
		if err != nil {
			log.Printf("WARN: error rate query failed for %s/%s: %v", slo.Service, slo.Name, err)
			continue
		}
		sliValue := 1.0 - errRate
		ch <- prometheus.MustNewConstMetric(c.descCompliance, prometheus.GaugeValue,
			sliValue, slo.Service, slo.Name)

		// Error budget remaining — approximated from the SLI vs target.
		budgetTotal := 1.0 - slo.Target
		budgetConsumed := slo.Target - sliValue
		var budgetRemaining float64
		if budgetTotal > 0 {
			budgetRemaining = 1.0 - (budgetConsumed / budgetTotal)
		}
		if budgetRemaining < 0 {
			budgetRemaining = 0
		}
		ch <- prometheus.MustNewConstMetric(c.descBudgetRemaining, prometheus.GaugeValue,
			budgetRemaining, slo.Service, slo.Name)

		// Per-window burn rates.
		sustainableRate := 1.0 - slo.Target // error rate that exactly consumes the budget in 30 days
		for _, w := range burnWindows {
			windowErrRate, err := c.dd.errorRate(ctx, slo.ErrorsMetric, slo.TotalMetric, w.seconds)
			if err != nil {
				log.Printf("WARN: burn rate query failed for %s/%s window=%s: %v", slo.Service, slo.Name, w.label, err)
				continue
			}
			var burnRate float64
			if sustainableRate > 0 {
				burnRate = windowErrRate / sustainableRate
			}
			ch <- prometheus.MustNewConstMetric(c.descBurnRate, prometheus.GaugeValue,
				burnRate, slo.Service, slo.Name, w.label)
		}
	}
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	configPath := flag.String("config", "slo-config.yaml", "Path to SLO config YAML")
	port := flag.Int("port", 9090, "Port to serve /metrics on")
	flag.Parse()

	for _, v := range []string{"DD_API_KEY", "DD_APP_KEY"} {
		if os.Getenv(v) == "" {
			log.Fatalf("ERROR: %s environment variable not set", v)
		}
	}

	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("ERROR: load config: %v", err)
	}

	dd := newDDClient(cfg.DatadogSite)
	collector := NewSLOCollector(cfg, dd)
	prometheus.MustRegister(collector)

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	addr := fmt.Sprintf(":%d", *port)
	log.Printf("slo_exporter listening on %s — scraping %d SLOs every %ds",
		addr, len(cfg.SLOs), cfg.ScrapeIntervalS)

	srv := &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
