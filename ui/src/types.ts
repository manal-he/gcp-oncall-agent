export type Status = "pending" | "running" | "awaiting_approval" | "completed" | "failed";

export interface TraceEvent {
  seq: number;
  type: string;
  at: string;
  data: Record<string, unknown>;
}

export interface Evidence {
  ref: string;
  tool: string;
  finding: string;
}

export interface RecommendedAction {
  kind: string;
  description: string;
  requires_approval: boolean;
  params: Record<string, string>;
}

export interface Report {
  title: string;
  summary: string;
  severity: string;
  root_cause_category: string;
  root_cause: string;
  timeline: { time: string; event: string; evidence_refs: string[] }[];
  evidence: Evidence[];
  hypotheses: { statement: string; status: string; evidence_refs: string[] }[];
  confidence: number;
  recommended_action: RecommendedAction;
  open_questions: string[];
  security_notes: string[];
}

export interface Snapshot {
  id: string;
  scenario: string | null;
  status: Status;
  duration_s: number;
  error: string | null;
  report: Report | null;
  approval: { approved: boolean; approver: string; comment: string } | null;
  action_result: { ok: boolean; result: unknown; error: string | null } | null;
  usage: { input_tokens: number; output_tokens: number; cost_usd: number; estimated: boolean } | null;
  steps: number;
  stop_reason: string | null;
  events: TraceEvent[];
}

export interface Scenario {
  name: string;
  title: string;
}
