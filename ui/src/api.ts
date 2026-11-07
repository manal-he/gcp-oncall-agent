import type { Scenario, Snapshot, TraceEvent } from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return (await res.json()) as T;
}

export const api = {
  scenarios: () => fetch("/api/scenarios").then((r) => json<Scenario[]>(r)),
  start: (scenario: string) =>
    fetch("/api/investigations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario }),
    }).then((r) => json<{ id: string }>(r)),
  get: (id: string) => fetch(`/api/investigations/${id}`).then((r) => json<Snapshot>(r)),
  decide: (id: string, approved: boolean, comment: string) =>
    fetch(`/api/investigations/${id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, comment }),
    }).then((r) => json<{ ok: boolean }>(r)),
  /** Subscribe to the live trace (Server-Sent Events). Returns an unsubscribe function. */
  stream: (id: string, onEvent: (e: TraceEvent) => void, onEnd: () => void) => {
    const es = new EventSource(`/api/investigations/${id}/events`);
    const handler = (msg: MessageEvent) => onEvent(JSON.parse(msg.data) as TraceEvent);
    for (const t of [
      "started", "plan", "llm_step", "tool_call", "blocked", "budget", "report",
      "approval_required", "approval", "action_executed", "completed", "failed",
    ]) {
      es.addEventListener(t, handler as EventListener);
    }
    es.addEventListener("end", () => {
      es.close();
      onEnd();
    });
    es.onerror = () => {
      es.close();
      onEnd();
    };
    return () => es.close();
  },
};
