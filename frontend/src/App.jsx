import { useCallback, useEffect, useRef, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

const API = "https://be7vs42u6g.execute-api.us-east-1.amazonaws.com";
const MAX_HISTORY = 20;

const ERROR_TYPES = [
  "Connection Timeout",
  "Validation Error",
  "Dependency Failure",
  "Unknown",
];

function StatCard({ label, value }) {
  return (
    <div className="rounded-lg bg-gray-800 border border-gray-700 px-5 py-4 text-center min-w-[160px]">
      <p className="text-xs font-medium uppercase tracking-wider text-gray-400">
        {label}
      </p>
      <p className="mt-1 text-3xl font-bold text-white">{value}</p>
    </div>
  );
}

function MetricChart({ title, data, dataKey, color }) {
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
      <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-4">
        {title}
      </h3>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" vertical={false} />
          <XAxis
            dataKey="time"
            tick={{ fill: "#6b7280", fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "#374151" }}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fill: "#6b7280", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            allowDecimals={false}
            width={32}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#1f2937",
              border: "1px solid #374151",
              borderRadius: "0.5rem",
              fontSize: 12,
            }}
            labelStyle={{ color: "#9ca3af" }}
            itemStyle={{ color }}
          />
          <Line
            type="monotone"
            dataKey={dataKey}
            stroke={color}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, fill: color, strokeWidth: 0 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function classificationColor(classification) {
  if (classification === "TIMEOUT" || classification === "DEPENDENCY_FAILURE" || classification === "UNKNOWN")
    return "text-yellow-400";
  return "text-red-400";
}

function actionBadge(action) {
  if (action === "RETRIED")
    return "bg-emerald-900/60 text-emerald-300 border-emerald-700";
  if (action === "DEAD")
    return "bg-red-900/60 text-red-300 border-red-700";
  return "bg-gray-700 text-gray-300 border-gray-600";
}

function classificationLabel(val) {
  const transient = ["TIMEOUT", "DEPENDENCY_FAILURE", "UNKNOWN"];
  return transient.includes(val) ? "TRANSIENT" : "PERMANENT";
}

export default function App() {
  const [stats, setStats] = useState({
    messages_processed: 0,
    messages_retried: 0,
    messages_dead: 0,
    alerts_sent: 0,
  });

  const [history, setHistory] = useState([]);
  const [body, setBody] = useState("");
  const [errorType, setErrorType] = useState(ERROR_TYPES[0]);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [activity, setActivity] = useState([]);
  const intervalRef = useRef(null);

  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(`${API}/stats`);
      if (!res.ok) return;
      const data = await res.json();
      setStats(data);
      setHistory((prev) => {
        const point = {
          time: new Date().toLocaleTimeString("en-US", { hour12: false }),
          retried: data.messages_retried,
          dead: data.messages_dead,
          alerts: data.alerts_sent,
        };
        const next = [...prev, point];
        return next.length > MAX_HISTORY ? next.slice(-MAX_HISTORY) : next;
      });
    } catch {
      /* network blip — will retry */
    }
  }, []);

  useEffect(() => {
    fetchStats();
    intervalRef.current = setInterval(fetchStats, 10_000);
    return () => clearInterval(intervalRef.current);
  }, [fetchStats]);

  async function handleSubmit(e) {
    e.preventDefault();
    setSubmitting(true);
    setResult(null);

    try {
      const res = await fetch(`${API}/inject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          body: body || '{"order_id": "123", "item": "book"}',
          error_type: errorType,
        }),
      });

      if (!res.ok) {
        const text = await res.text();
        setResult({ error: `HTTP ${res.status}: ${text}` });
        return;
      }

      const data = await res.json();
      setResult(data);

      setActivity((prev) => {
        const next = [
          {
            message_id: data.message_id,
            error_type: data.error_type,
            classification: data.classification,
            action: data.action,
            timestamp: new Date().toLocaleTimeString(),
          },
          ...prev,
        ];
        return next.slice(0, 5);
      });

      fetchStats();
    } catch (err) {
      setResult({ error: err.message });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900/80 backdrop-blur">
        <div className="mx-auto max-w-5xl px-6 py-6">
          <h1 className="text-2xl font-bold tracking-tight text-white">
            DLQ Monitor
          </h1>
          <p className="mt-1 text-sm text-gray-400">
            Dead Letter Queue Failure Triage
          </p>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-8 space-y-8">
        {/* Live Stats */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
              Live Stats
            </h2>
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
          </div>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <StatCard label="Processed" value={stats.messages_processed} />
            <StatCard label="Retried" value={stats.messages_retried} />
            <StatCard label="Dead" value={stats.messages_dead} />
            <StatCard label="Alerts Sent" value={stats.alerts_sent} />
          </div>
        </section>

        {/* Metrics Charts */}
        <section>
          <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-4">
            Metrics Over Time
          </h2>
          <div className="grid gap-4 lg:grid-cols-3">
            <MetricChart
              title="Messages Retried"
              data={history}
              dataKey="retried"
              color="#2dd4bf"
            />
            <MetricChart
              title="Messages Dead"
              data={history}
              dataKey="dead"
              color="#f87171"
            />
            <MetricChart
              title="Alerts Sent"
              data={history}
              dataKey="alerts"
              color="#facc15"
            />
          </div>
        </section>

        <div className="grid gap-8 lg:grid-cols-2">
          {/* Submit Form */}
          <section className="rounded-xl border border-gray-800 bg-gray-900 p-6">
            <h2 className="text-lg font-semibold text-white mb-5">
              Submit Failure
            </h2>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1.5">
                  Message Body
                </label>
                <textarea
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  placeholder='{"order_id": "123", "item": "book"}'
                  rows={4}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-4 py-2.5 text-sm text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 font-mono"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1.5">
                  Error Type
                </label>
                <select
                  value={errorType}
                  onChange={(e) => setErrorType(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-4 py-2.5 text-sm text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  {ERROR_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="w-full rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {submitting ? "Submitting..." : "Submit Failure"}
              </button>
            </form>

            {/* Result */}
            {result && (
              <div className="mt-5 rounded-lg border border-gray-700 bg-gray-800 p-4 text-sm space-y-2">
                {result.error ? (
                  <p className="text-red-400">{result.error}</p>
                ) : (
                  <>
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400">Message ID</span>
                      <span className="font-mono text-xs text-gray-300 truncate max-w-[220px]">
                        {result.message_id}
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400">Classification</span>
                      <span className={classificationColor(result.classification)}>
                        {classificationLabel(result.classification)}{" "}
                        <span className="text-gray-500">
                          ({result.classification})
                        </span>
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400">Action</span>
                      <span
                        className={`rounded-full border px-3 py-0.5 text-xs font-medium ${actionBadge(result.action)}`}
                      >
                        {result.action}
                      </span>
                    </div>
                  </>
                )}
              </div>
            )}
          </section>

          {/* Activity Feed */}
          <section className="rounded-xl border border-gray-800 bg-gray-900 p-6">
            <h2 className="text-lg font-semibold text-white mb-5">
              Recent Activity
            </h2>
            {activity.length === 0 ? (
              <p className="text-sm text-gray-500 italic">
                No activity yet. Submit a failure to get started.
              </p>
            ) : (
              <ul className="space-y-3">
                {activity.map((item, i) => (
                  <li
                    key={`${item.message_id}-${i}`}
                    className="rounded-lg border border-gray-700 bg-gray-800 p-4 text-sm space-y-1.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-xs text-gray-400 truncate max-w-[200px]">
                        {item.message_id}
                      </span>
                      <span className="text-xs text-gray-500">
                        {item.timestamp}
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-gray-300">{item.error_type}</span>
                      <span className={classificationColor(item.classification)}>
                        {classificationLabel(item.classification)}
                      </span>
                    </div>
                    <div className="flex justify-end">
                      <span
                        className={`rounded-full border px-3 py-0.5 text-xs font-medium ${actionBadge(item.action)}`}
                      >
                        {item.action}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}
