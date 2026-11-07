import type { Report } from "../types";

export function ReportView({ report }: { report: Report }) {
  const pct = Math.round(report.confidence * 100);
  const a = report.recommended_action;
  return (
    <div className="space-y-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded bg-slate-900 px-2 py-0.5 text-xs font-semibold text-white">{report.severity}</span>
          <span className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs">{report.root_cause_category}</span>
        </div>
        <h2 className="mt-2 text-lg font-semibold">{report.title}</h2>
        <p className="mt-1 text-sm text-slate-600">{report.summary}</p>
      </div>

      <div>
        <div className="flex justify-between text-xs text-slate-500">
          <span>confidence</span>
          <span>{pct}%</span>
        </div>
        <div className="mt-1 h-1.5 rounded bg-slate-100">
          <div className="h-1.5 rounded bg-emerald-500" style={{ width: `${pct}%` }} />
        </div>
      </div>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Root cause</h3>
        <p className="mt-1 text-sm">{report.root_cause}</p>
      </section>

      {report.timeline.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Timeline</h3>
          <ul className="mt-1 space-y-1 text-sm">
            {report.timeline.map((t, i) => (
              <li key={i} className="flex gap-2">
                <span className="shrink-0 font-mono text-xs text-slate-500">{t.time.slice(11, 19)}</span>
                <span>
                  {t.event} <Refs refs={t.evidence_refs} />
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Evidence</h3>
        <ul className="mt-1 space-y-2 text-sm">
          {report.evidence.map((e, i) => (
            <li key={i}>
              <Refs refs={[e.ref]} /> <span className="font-mono text-xs text-slate-500">{e.tool}</span>
              <p className="break-words text-slate-700">{e.finding}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className="rounded border border-slate-200 p-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Recommended action</h3>
        <p className="mt-1 text-sm">
          <span className="font-mono text-xs">{a.kind}</span> - {a.description}
        </p>
        {Object.keys(a.params).length > 0 && (
          <pre className="mt-2 overflow-auto rounded bg-slate-50 p-2 text-xs">{JSON.stringify(a.params, null, 2)}</pre>
        )}
      </section>

      {report.security_notes.length > 0 && (
        <section className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <h3 className="text-xs font-semibold uppercase tracking-wide">Security notes</h3>
          <ul className="mt-1 list-disc pl-5">
            {report.security_notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </section>
      )}

      {report.open_questions.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Open questions</h3>
          <ul className="mt-1 list-disc pl-5 text-sm">
            {report.open_questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Refs({ refs }: { refs: string[] }) {
  return (
    <>
      {refs.map((r) => (
        <span key={r} className="mr-1 rounded bg-sky-50 px-1 font-mono text-xs text-sky-700">
          {r}
        </span>
      ))}
    </>
  );
}
