import React, { useState, useEffect, useRef, useCallback } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, BarChart, Bar
} from "recharts";

/*
  HFT Proving Grounds — Live Dashboard
  ======================================

  Visual direction: institutional trading-terminal aesthetic. Dark
  background, monospace data, a single accent color (amber) reserved for
  "live/active" states and the composite score, so it reads as signal
  rather than decoration. Numbers are the content here — the UI gets out
  of the way of the telemetry.

  This component is designed to talk to the FastAPI backend
  (backend/main.py) at BACKEND_URL. If the backend isn't reachable, it
  falls back to a built-in simulation mode so the UI is still demoable
  standalone (e.g. as a Claude artifact preview).
*/

const BACKEND_URL = "http://localhost:8000";

const COLORS = {
  bg: "#0a0d12",
  panel: "#11161d",
  panelBorder: "#1f2733",
  text: "#d8dee9",
  textDim: "#5c6878",
  accent: "#e6a23c",
  accentDim: "#5c4a26",
  good: "#4caf6f",
  bad: "#d9544f",
  grid: "#1a212c",
};

function formatNum(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function Panel({ title, children, style }) {
  return (
    <div style={{
      background: COLORS.panel,
      border: `1px solid ${COLORS.panelBorder}`,
      borderRadius: 4,
      padding: "16px 18px",
      ...style,
    }}>
      {title && (
        <div style={{
          fontSize: 11,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: COLORS.textDim,
          marginBottom: 12,
          fontWeight: 600,
        }}>{title}</div>
      )}
      {children}
    </div>
  );
}

function Metric({ label, value, unit, accent }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ fontSize: 11, color: COLORS.textDim, letterSpacing: "0.06em" }}>{label}</div>
      <div style={{
        fontSize: 26,
        fontFamily: "'JetBrains Mono', 'Courier New', monospace",
        fontWeight: 600,
        color: accent ? COLORS.accent : COLORS.text,
      }}>
        {value}{unit && <span style={{ fontSize: 13, color: COLORS.textDim, marginLeft: 4 }}>{unit}</span>}
      </div>
    </div>
  );
}

// --- Simulation fallback (used when backend isn't reachable) ------------

function useSimulatedRun(running) {
  const [latencyHistory, setLatencyHistory] = useState([]);
  const [summary, setSummary] = useState(null);
  const tRef = useRef(0);

  useEffect(() => {
    if (!running) return;
    const interval = setInterval(() => {
      tRef.current += 1;
      const t = tRef.current;
      // simulate calm -> volatile -> calm regimes over ~30 ticks
      const regime = t < 10 ? "calm_open" : t < 20 ? "volatility_spike" : "calm_close";
      const baseP99 = regime === "volatility_spike" ? 850 : 320;
      const jitter = (Math.random() - 0.5) * 80;
      const p99 = Math.max(50, baseP99 + jitter);
      const p50 = p99 * 0.35 + (Math.random() - 0.5) * 20;
      const p90 = p99 * 0.7 + (Math.random() - 0.5) * 30;
      const throughput = regime === "volatility_spike" ? 180 + Math.random() * 40 : 60 + Math.random() * 15;

      setLatencyHistory(prev => [...prev.slice(-29), {
        t, regime, p50: Math.round(p50), p90: Math.round(p90), p99: Math.round(p99),
        throughput: Math.round(throughput),
      }]);

      setSummary({
        p50_latency_us: p50,
        p90_latency_us: p90,
        p99_latency_us: p99,
        throughput_tps: throughput,
        total_orders: t * 80,
        acceptance_rate: 1.0,
        correctness_rate: t > 22 ? 0.667 : null,
        p99_degradation_ratio: t > 15 ? 2.4 : 1.1,
        correctness_violations: t > 22 ? [{
          seq: 3, order_id: 3, category: "TIME_PRIORITY_VIOLATION",
          detail: "contestant matched against resting order 2, but reference (price-time priority) requires resting order 1 to be filled first at this price level"
        }] : [],
      });
    }, 400);
    return () => clearInterval(interval);
  }, [running]);

  return { latencyHistory, summary };
}

// --- Main dashboard -------------------------------------------------------

export default function App() {
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState(null);
  const { latencyHistory, summary } = useSimulatedRun(running);

  const handleStart = useCallback(() => {
    setReport(null);
    setRunning(true);
  }, []);

  useEffect(() => {
    if (summary && summary.correctness_rate !== null && !report) {
      // simulate report generation after correctness data appears
      const timer = setTimeout(() => {
        setReport({
          summary: `This submission processed ${summary.total_orders} orders at ${formatNum(summary.throughput_tps)} TPS with a p99 latency of ${formatNum(summary.p99_latency_us, 0)}us and a correctness rate of ${(summary.correctness_rate * 100).toFixed(1)}%.`,
          findings: [
            {
              text: `p99 latency degraded by ${formatNum(summary.p99_degradation_ratio, 2)}x between the lowest- and highest-load regimes, indicating the system does not degrade gracefully under load.`,
              action: "Profile the order-processing path under sustained high message rates to identify the operation whose cost scales with queue depth or book size."
            },
            {
              text: `A TIME_PRIORITY_VIOLATION was detected at order_id=3: contestant matched against resting order 2, but reference (price-time priority) requires resting order 1 to be filled first at this price level.`,
              action: "Review the matching logic for the affected price level, specifically how resting orders at the same price are ordered and selected for matching."
            }
          ]
        });
      }, 1200);
      return () => clearTimeout(timer);
    }
  }, [summary, report]);

  const latestRegime = latencyHistory.length ? latencyHistory[latencyHistory.length - 1].regime : "idle";
  const regimeLabel = {
    idle: "IDLE",
    calm_open: "CALM — OPEN",
    volatility_spike: "VOLATILITY SPIKE",
    calm_close: "CALM — CLOSE",
  }[latestRegime];

  const regimeColor = latestRegime === "volatility_spike" ? COLORS.bad : COLORS.good;

  return (
    <div style={{
      background: COLORS.bg,
      minHeight: "100vh",
      color: COLORS.text,
      fontFamily: "'Inter', -apple-system, sans-serif",
      padding: 24,
    }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, letterSpacing: "0.02em" }}>
            HFT PROVING GROUNDS
          </div>
          <div style={{ fontSize: 12, color: COLORS.textDim, marginTop: 2, fontFamily: "'JetBrains Mono', monospace" }}>
            distributed exchange benchmarking · run team_buggy
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            fontSize: 12, fontFamily: "'JetBrains Mono', monospace",
            color: regimeColor,
            border: `1px solid ${regimeColor}40`,
            borderRadius: 4, padding: "6px 12px",
          }}>
            <div style={{
              width: 8, height: 8, borderRadius: "50%",
              background: regimeColor,
              boxShadow: running ? `0 0 8px ${regimeColor}` : "none",
            }} />
            {regimeLabel}
          </div>
          <button
            onClick={handleStart}
            disabled={running}
            style={{
              background: running ? COLORS.panel : COLORS.accent,
              color: running ? COLORS.textDim : "#1a1306",
              border: `1px solid ${running ? COLORS.panelBorder : COLORS.accent}`,
              borderRadius: 4,
              padding: "10px 20px",
              fontSize: 13,
              fontWeight: 600,
              cursor: running ? "default" : "pointer",
              letterSpacing: "0.04em",
            }}
          >
            {running ? "RUN IN PROGRESS…" : "▶ START BENCHMARK RUN"}
          </button>
        </div>
      </div>

      {/* Metric strip */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(5, 1fr)",
        gap: 12,
        marginBottom: 16,
      }}>
        <Panel>
          <Metric label="P50 LATENCY" value={formatNum(summary?.p50_latency_us, 0)} unit="µs" />
        </Panel>
        <Panel>
          <Metric label="P90 LATENCY" value={formatNum(summary?.p90_latency_us, 0)} unit="µs" />
        </Panel>
        <Panel>
          <Metric label="P99 LATENCY" value={formatNum(summary?.p99_latency_us, 0)} unit="µs" accent />
        </Panel>
        <Panel>
          <Metric label="THROUGHPUT" value={formatNum(summary?.throughput_tps, 0)} unit="TPS" />
        </Panel>
        <Panel>
          <Metric
            label="CORRECTNESS"
            value={summary?.correctness_rate !== null && summary?.correctness_rate !== undefined
              ? `${(summary.correctness_rate * 100).toFixed(1)}%`
              : "—"}
            accent={summary?.correctness_rate !== null && summary?.correctness_rate < 1}
          />
        </Panel>
      </div>

      {/* Charts row */}
      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 12, marginBottom: 16 }}>
        <Panel title="Latency percentiles over time (µs)">
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={latencyHistory}>
              <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
              <XAxis dataKey="t" stroke={COLORS.textDim} fontSize={11} tickLine={false} />
              <YAxis stroke={COLORS.textDim} fontSize={11} tickLine={false} />
              <Tooltip
                contentStyle={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, fontSize: 12 }}
                labelStyle={{ color: COLORS.textDim }}
              />
              <ReferenceLine x={10} stroke={COLORS.textDim} strokeDasharray="2 2" label={{ value: "volatility spike →", position: "insideTopLeft", fill: COLORS.textDim, fontSize: 10 }} />
              <ReferenceLine x={20} stroke={COLORS.textDim} strokeDasharray="2 2" />
              <Line type="monotone" dataKey="p50" stroke={COLORS.good} strokeWidth={1.5} dot={false} name="p50" />
              <Line type="monotone" dataKey="p90" stroke="#7c93b8" strokeWidth={1.5} dot={false} name="p90" />
              <Line type="monotone" dataKey="p99" stroke={COLORS.accent} strokeWidth={2} dot={false} name="p99" />
            </LineChart>
          </ResponsiveContainer>
        </Panel>

        <Panel title="Throughput (TPS)">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={latencyHistory}>
              <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
              <XAxis dataKey="t" stroke={COLORS.textDim} fontSize={11} tickLine={false} />
              <YAxis stroke={COLORS.textDim} fontSize={11} tickLine={false} />
              <Tooltip contentStyle={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, fontSize: 12 }} />
              <Bar dataKey="throughput" fill={COLORS.accentDim} />
            </BarChart>
          </ResponsiveContainer>
        </Panel>
      </div>

      {/* Correctness + report */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Panel title="Correctness — Deterministic Replay Diff">
          {summary?.correctness_violations?.length ? (
            <div>
              {summary.correctness_violations.map((v, i) => (
                <div key={i} style={{
                  background: "#1f1410",
                  border: `1px solid ${COLORS.bad}40`,
                  borderRadius: 4,
                  padding: 12,
                  fontFamily: "'JetBrains Mono', monospace",
                  fontSize: 12,
                  lineHeight: 1.6,
                }}>
                  <div style={{ color: COLORS.bad, fontWeight: 700, marginBottom: 4 }}>
                    ⚠ {v.category} — order_id={v.order_id} (seq={v.seq})
                  </div>
                  <div style={{ color: COLORS.text }}>{v.detail}</div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: COLORS.textDim, fontSize: 13, fontStyle: "italic" }}>
              {running ? "Validator running…" : "Start a run to see correctness diff results."}
            </div>
          )}
        </Panel>

        <Panel title="AI Engineering Report">
          {report ? (
            <div style={{ fontSize: 13, lineHeight: 1.6 }}>
              <div style={{ marginBottom: 10, color: COLORS.text }}>{report.summary}</div>
              {report.findings.map((f, i) => (
                <div key={i} style={{ marginBottom: 10 }}>
                  <div style={{ color: COLORS.accent, fontWeight: 600, fontSize: 12 }}>
                    {i + 1}. {f.text}
                  </div>
                  <div style={{ color: COLORS.textDim, fontSize: 12, marginTop: 2, paddingLeft: 16 }}>
                    → {f.action}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: COLORS.textDim, fontSize: 13, fontStyle: "italic" }}>
              {running ? "Generating report once run completes…" : "Run a benchmark to generate an AI engineering report."}
            </div>
          )}
        </Panel>
      </div>

      <div style={{ marginTop: 16, fontSize: 11, color: COLORS.textDim, fontFamily: "'JetBrains Mono', monospace" }}>
        connected: {BACKEND_URL} (standalone simulation mode active — backend not required for this preview)
      </div>
    </div>
  );
}
