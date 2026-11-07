import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { ApprovalPanel } from "./components/ApprovalPanel";
import { ReportView } from "./components/ReportView";
import { Trace } from "./components/Trace";
import type { Scenario, Snapshot, TraceEvent } from "./types";

const STATUS_CLS: Record<string, string> = {
  pending: "bg-slate-200 text-slate-700",
  running: "bg-sky-100 text-sky-800",
  awaiting_approval: "bg-orange-100 text-orange-800",
  completed: "bg-emerald-100 text-emerald-800",
  failed: "bg-red-100 text-red-800",
};

export default function App() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [scenario, setScenario] = useState("bad-deploy");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const unsubscribe = useRef<(() => void) | null>(null);

  useEffect(() => {
    api.scenarios().then(setScenarios).catch((e) => setError(String(e)));
    return () => unsubscribe.current?.();
  }, []);

  const refresh = useCallback(async (id: string) => setSnap(await api.get(id)), []);

  const start = async () => {
    setError(null);
    setEvents([]);
    setSnap(null);
    unsubscribe.current?.();
    try {
      const { id } = await api.start(scenario);
      unsubscribe.current = api.stream(
        id,
        (e) => {
          setEvents((prev) => (prev.some((p) => p.seq === e.seq) ? prev : [...prev, e]));
          if (["report", "approval_required", "approval", "action_executed", "completed", "failed"].includes(e.type)) {
            void refresh(id);
          }
        },
        () => void refresh(id),
      );
    } catch (e) {
      setError(String(e));
    }
  };

  const decide = async (approved: boolean, comment: string) => {
    if (!snap) return;
    await api.decide(snap.id, approved, comment);
    await refresh(snap.id);
  };

  const status = snap?.status ?? (events.length ? "running" : null);

  return (
    <div className="mx-auto max-w-7xl px-4 py-6">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 pb-4">
        <div>
          <h1 className="text-xl font-semibold">gcp-oncall-agent</h1>
          <p className="text-sm text-slate-500">AI on-call investigations over MCP, with human approval for writes</p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={scenario}
            onChange={(e) => setScenario(e.target.value)}
            className="rounded border border-slate-300 bg-white px-3 py-2 text-sm"
          >
            {scenarios.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name} - {s.title}
              </option>
            ))}
          </select>
          <button
            onClick={start}
            disabled={status === "running"}
            className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          >
            Investigate
          </button>
        </div>
      </header>

      {error && <p className="mt-4 rounded bg-red-50 p-3 text-sm text-red-800">{error}</p>}

      {status && (
        <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
          <span className={`rounded px-2 py-0.5 font-medium ${STATUS_CLS[status] ?? ""}`}>{status}</span>
          {snap && (
            <span className="text-slate-500">
              {snap.steps} steps - {snap.usage?.input_tokens ?? 0} / {snap.usage?.output_tokens ?? 0} tokens
              {snap.usage?.estimated ? " (estimated)" : ""} - {snap.duration_s}s
              {snap.stop_reason ? ` - stopped: ${snap.stop_reason}` : ""}
            </span>
          )}
        </div>
      )}

      <main className="mt-4 grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">Investigation trace</h2>
          <Trace events={events} />
        </section>
        <aside className="space-y-4">
          {snap?.status === "awaiting_approval" && snap.report && (
            <ApprovalPanel action={snap.report.recommended_action} confidence={snap.report.confidence} onDecide={decide} />
          )}
          {snap?.approval && (
            <div
              className={`rounded-lg p-3 text-sm ${snap.approval.approved ? "bg-emerald-50 text-emerald-900" : "bg-slate-100"}`}
            >
              {snap.approval.approved ? "Approved" : "Rejected"} by {snap.approval.approver}
              {snap.action_result &&
                (snap.action_result.ok ? " - rollback executed." : ` - action failed: ${snap.action_result.error}`)}
            </div>
          )}
          <section className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">Incident report</h2>
            {snap?.report ? <ReportView report={snap.report} /> : <p className="text-sm text-slate-500">Pending.</p>}
          </section>
        </aside>
      </main>
    </div>
  );
}
