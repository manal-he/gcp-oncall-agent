import { useState } from "react";
import type { RecommendedAction } from "../types";

interface Props {
  action: RecommendedAction;
  confidence: number;
  onDecide: (approved: boolean, comment: string) => Promise<void>;
}

export function ApprovalPanel({ action, confidence, onDecide }: Props) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const decide = async (approved: boolean) => {
    setBusy(true);
    try {
      await onDecide(approved, comment);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border-2 border-orange-300 bg-orange-50 p-4">
      <h3 className="font-semibold text-orange-900">Approval required</h3>
      <p className="mt-1 text-sm text-orange-900">
        The agent recommends a write action. Nothing changes unless you approve it; the approval token is bound to
        exactly these parameters and expires in minutes.
      </p>
      <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-500">action</dt>
        <dd className="font-mono">{action.kind}</dd>
        {Object.entries(action.params).map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-slate-500">{k}</dt>
            <dd className="font-mono">{v}</dd>
          </div>
        ))}
        <dt className="text-slate-500">confidence</dt>
        <dd>{Math.round(confidence * 100)}%</dd>
      </dl>
      <textarea
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder="Comment (optional, recorded in the audit trail)"
        maxLength={500}
        className="mt-3 w-full rounded border border-orange-200 bg-white p-2 text-sm"
        rows={2}
      />
      <div className="mt-3 flex gap-2">
        <button
          disabled={busy}
          onClick={() => decide(true)}
          className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          Approve rollback
        </button>
        <button
          disabled={busy}
          onClick={() => decide(false)}
          className="rounded border border-slate-300 bg-white px-4 py-2 text-sm font-medium hover:bg-slate-50 disabled:opacity-50"
        >
          Reject
        </button>
      </div>
    </div>
  );
}
