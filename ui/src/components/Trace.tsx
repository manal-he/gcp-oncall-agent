import { useState } from "react";
import type { TraceEvent } from "../types";

const LABEL: Record<string, { text: string; cls: string }> = {
  started: { text: "start", cls: "bg-slate-200 text-slate-700" },
  plan: { text: "plan", cls: "bg-indigo-100 text-indigo-700" },
  llm_step: { text: "think", cls: "bg-violet-100 text-violet-700" },
  tool_call: { text: "tool", cls: "bg-sky-100 text-sky-700" },
  blocked: { text: "blocked", cls: "bg-red-100 text-red-700" },
  budget: { text: "budget", cls: "bg-amber-100 text-amber-800" },
  report: { text: "report", cls: "bg-emerald-100 text-emerald-700" },
  approval_required: { text: "approval", cls: "bg-orange-100 text-orange-700" },
  approval: { text: "decision", cls: "bg-orange-100 text-orange-700" },
  action_executed: { text: "action", cls: "bg-emerald-200 text-emerald-800" },
  completed: { text: "done", cls: "bg-slate-200 text-slate-700" },
  failed: { text: "failed", cls: "bg-red-200 text-red-800" },
};

function summary(e: TraceEvent): string {
  const d = e.data as Record<string, any>;
  switch (e.type) {
    case "started":
      return `scenario ${d.scenario} - ${d.llm}`;
    case "plan":
      return d.text;
    case "llm_step":
      return d.tool_calls?.length ? `${d.text} -> ${d.tool_calls.map((c: any) => c.name).join(", ")}` : d.text;
    case "tool_call":
      return `${d.ref} ${d.tool}(${Object.entries(d.args ?? {})
        .filter(([k]) => k !== "project_id")
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
        .join(", ")}) ${d.ok ? `${d.duration_ms} ms` : `error: ${d.error}`}`;
    case "blocked":
      return `${d.ref} ${d.tool} was requested by the model and blocked (not in the read-only allowlist)`;
    case "budget":
      return d.reason;
    case "report":
      return `${d.root_cause_category} - confidence ${Math.round((d.confidence ?? 0) * 100)}%`;
    case "approval_required":
      return `waiting for a human: ${d.action?.description ?? ""}`;
    case "approval":
      return `${d.approved ? "approved" : "rejected"} by ${d.approver}`;
    case "action_executed":
      return d.ok ? `rollback executed: ${JSON.stringify((d.result as any)?.traffic)}` : `action failed: ${d.error}`;
    case "failed":
      return String(d.error);
    default:
      return "";
  }
}

function Row({ e }: { e: TraceEvent }) {
  const [open, setOpen] = useState(false);
  const label = LABEL[e.type] ?? { text: e.type, cls: "bg-slate-100" };
  const d = e.data as Record<string, any>;
  const expandable = e.type === "tool_call" && d.result != null;
  return (
    <li className="py-2">
      <div className="flex items-start gap-3">
        <span className={`mt-0.5 w-20 shrink-0 rounded px-2 py-0.5 text-center text-xs font-medium ${label.cls}`}>
          {label.text}
        </span>
        <div className="min-w-0 flex-1">
          <p className="break-words font-mono text-[13px] leading-5 text-slate-800">{summary(e)}</p>
          {d.suspected_injection > 0 && (
            <p className="mt-1 text-xs font-medium text-amber-700">
              {d.suspected_injection} log entr{d.suspected_injection > 1 ? "ies" : "y"} flagged as possible prompt
              injection (treated as data)
            </p>
          )}
          {expandable && (
            <button onClick={() => setOpen(!open)} className="mt-1 text-xs text-sky-700 hover:underline">
              {open ? "hide result" : "show result"}
            </button>
          )}
          {open && (
            <pre className="mt-2 max-h-80 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
              {JSON.stringify(d.result, null, 2)}
            </pre>
          )}
        </div>
        <time className="shrink-0 text-xs text-slate-400">{new Date(e.at).toLocaleTimeString()}</time>
      </div>
    </li>
  );
}

export function Trace({ events }: { events: TraceEvent[] }) {
  if (!events.length) return <p className="text-sm text-slate-500">No events yet.</p>;
  return (
    <ol className="divide-y divide-slate-100">
      {events.map((e) => (
        <Row key={e.seq} e={e} />
      ))}
    </ol>
  );
}
