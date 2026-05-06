package main

import (
	"encoding/json"
	"log"
	"math/rand"
	"net/http"
	"os"
	"sync"
	"time"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var version = "1.0.0"
var startTime = time.Now()

var (
	httpRequestsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "http_requests_total",
			Help: "Total number of HTTP requests",
		},
		[]string{"method", "path", "status_code"},
	)

	httpRequestDuration = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "http_request_duration_seconds",
			Help:    "HTTP request latency",
			Buckets: prometheus.DefBuckets, // standard buckets
		},
		[]string{"method", "path"},
	)

	appUptime = prometheus.NewGaugeFunc(
		prometheus.GaugeOpts{
			Name: "app_uptime_seconds",
			Help: "Application uptime in seconds",
		},
		func() float64 {
			return time.Since(startTime).Seconds()
		},
	)

	appMode = prometheus.NewGaugeFunc(
		prometheus.GaugeOpts{
			Name: "app_mode",
			Help: "Application mode (0=stable, 1=canary)",
		},
		func() float64 {
			if isCanary() {
				return 1
			}
			return 0
		},
	)

	chaosActive = prometheus.NewGaugeFunc(
		prometheus.GaugeOpts{
			Name: "chaos_active",
			Help: "Chaos state (0=none, 1=slow, 2=error)",
		},
		func() float64 {
			chaos.mu.RLock()
			defer chaos.mu.RUnlock()
			switch chaos.mode {
			case "slow":
				return 1
			case "error":
				return 2
			default:
				return 0
			}
		},
	)
)


type responseRecorder struct {
	http.ResponseWriter
	statusCode int
}

func (r *responseRecorder) WriteHeader(code int) {
	r.statusCode = code
	r.ResponseWriter.WriteHeader(code)
}

func metricsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()

		rec := &responseRecorder{
			ResponseWriter: w,
			statusCode:     200,
		}

		next.ServeHTTP(rec, r)

		duration := time.Since(start).Seconds()

		path := r.URL.Path

		httpRequestsTotal.WithLabelValues(
			r.Method,
			path,
			http.StatusText(rec.statusCode),
		).Inc()

		httpRequestDuration.WithLabelValues(
			r.Method,
			path,
		).Observe(duration)
	})
}

type chaosState struct {
	mu			sync.RWMutex
	mode		string
	duration 	int
	rate		float64
}

var chaos = &chaosState{}

func (c *chaosState) set(mode string, duration int, rate float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.mode = mode
	c.duration = duration
	c.rate = rate
}
func (c *chaosState) apply(w http.ResponseWriter) (abort bool) {
	c.mu.RLock()
	mode := c.mode
	duration := c.duration
	rate := c.rate
	c.mu.RUnlock()

	switch mode{
	case "slow":
		time.Sleep(time.Duration(duration) * time.Second)
	case "error":
		if rand.Float64() < rate {
			http.Error(w, `{"error": "chaos error injection"}`, http.StatusInternalServerError)
			return true
		}
	}
	return false
}
// Get mode helper
func getMode() string {
	mode := os.Getenv("MODE")
	if mode == "canary" {
		return "canary"
	}
	return "stable"
}

func isCanary() bool {
	return  getMode() == "canary"
}

func addCanaryHeader(w http.ResponseWriter) {
	if isCanary() {
		w.Header().Set("X-Mode", "canary")
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	addCanaryHeader(w)
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// CORS middleware
func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		
		next.ServeHTTP(w, r)
	})
}

// Handlers
// GET /
func rootHandler(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	if isCanary() && chaos.apply(w) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"message": "Welcome to the API service",
		"mode": getMode(),
		"version": version,
		"timestamp": time.Now().UTC().Format(time.RFC3339),
	})
}

// GET /healthz
func healthzHandler(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"uptime": time.Since(startTime).Seconds(),
	})
}

// POST /chaos
func chaosHandler(w http.ResponseWriter, r *http.Request) {
	if !isCanary() {
		http.Error(w, `{"error": "chaos endpoint only available in canary mode"}`, http.StatusForbidden)
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, `{"error": "method not allowed"}`, http.StatusMethodNotAllowed)
		return
	}
	
	var body struct {
		Mode		string	`json:"mode"`
		Duration	int		`json:"duration"`
		Rate		float64	`json:"rate"`
	}

	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, `{"error": "invalid JSON body"}`, http.StatusBadRequest)
		return
	}

	switch body.Mode {
	case "slow":
		if body.Duration <= 0 {
			http.Error(w, `{"error": "duration must be > 0"}`, http.StatusBadRequest)
			return
		}
		chaos.set("slow", body.Duration, 0)
	case "error":
		if body.Rate < 0 || body.Rate > 1 {
			http.Error(w, `{"error": "rate must be between 0 and 1"}`, http.StatusBadRequest)
			return
		}
		chaos.set("error", 0, body.Rate)
	case "recover":
		chaos.set("", 0, 0)
	default:
		http.Error(w, `{"error": "unknown chaos mode; use slow|error|recover"}`, http.StatusBadRequest)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"message": "chaos state updated",
		"applied": body.Mode,
	})
}

// MAIN

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/", rootHandler)
	mux.HandleFunc("/healthz", healthzHandler)
	mux.HandleFunc("/chaos", chaosHandler)
	mux.Handle("/metrics", promhttp.Handler())

	prometheus.MustRegister(
	httpRequestsTotal,
	httpRequestDuration,
	appUptime,
	appMode,
	chaosActive,
)
	handler := corsMiddleware(metricsMiddleware(mux))
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	log.Printf("starting API service mode=%s version=%s port=%s", getMode(), version, port)
	if err := http.ListenAndServe(":"+port, handler); err != nil {
		log.Fatalf("server error: %v", err)
	}

	
}
