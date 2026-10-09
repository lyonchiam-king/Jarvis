import React from 'react';
import { ExternalLink, AlertTriangle, Info } from 'lucide-react';
import { Summary, Stage } from '../types';
import { Empty } from './CampaignsView';

interface ResultsViewProps {
  summary: Summary | null;
}

/**
 * What came of the work.
 *
 * Everything here is counted from the Sheet and nothing is modelled,
 * projected or averaged. Two of these numbers -- Replied and Closed -- are
 * stages a person sets by hand, so the page says so rather than presenting
 * them as something observed: a reply rate that quietly means "rows Lyon
 * remembered to update" is worse than no reply rate, because it gets
 * believed.
 *
 * There is deliberately no chart over time. The Sheet keeps a current
 * stage per lead, not a history of when each one changed, so any trend
 * line would be drawn from data that does not exist.
 */
export const ResultsView: React.FC<ResultsViewProps> = ({ summary }) => {
  const by = summary?.by_stage;
  const sent = by?.Sent || 0;
  const replied = by?.Replied || 0;
  const closed = by?.Closed || 0;
  const total = summary?.total || 0;

  const funnel: { label: string; stage: Stage; count: number }[] = [
    { label: 'Found', stage: 'Discovered', count: total },
    { label: 'Researched', stage: 'Researched', count: total - (summary?.to_research || 0) },
    { label: 'Site built', stage: 'Site Built', count: by?.['Site Built'] || 0 },
    { label: 'Message sent', stage: 'Sent', count: sent },
    { label: 'Replied', stage: 'Replied', count: replied },
    { label: 'Closed', stage: 'Closed', count: closed },
  ];

  if (!total) {
    return (
      <div id="results-view" className="space-y-5">
        <Header sheetUrl={summary?.sheet_url} />
        <div className="rounded-2xl border border-slate-800 bg-slate-900">
          <Empty
            title="Nothing to measure yet"
            body="Once leads have been found and messages sent, the counts from your Sheet appear here. No numbers are estimated -- an empty screen means an empty Sheet."
          />
        </div>
      </div>
    );
  }

  const widest = Math.max(...funnel.map((f) => f.count), 1);

  return (
    <div id="results-view" className="space-y-5">
      <Header sheetUrl={summary?.sheet_url} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Big label="Messages sent" value={sent} />
        <Big label="Replies" value={replied} note="You mark these" />
        <Big label="Closed" value={closed} note="You mark these" />
        <Big
          label="Disqualified"
          value={summary?.disqualified || 0}
          muted
          note="Not reachable, or removed"
        />
      </div>

      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5">
        <h2 className="text-sm font-semibold text-slate-100">
          Where everyone currently sits
        </h2>
        <p className="text-[12px] text-slate-500 mt-0.5 mb-5">
          Each lead counts once, at the stage it has reached today.
        </p>
        <div className="space-y-3">
          {funnel.map((row) => (
            <div key={row.stage} className="flex items-center gap-3">
              <span className="w-28 shrink-0 text-[12px] text-slate-400">
                {row.label}
              </span>
              <span className="flex-1 h-2 rounded-full bg-slate-800 overflow-hidden">
                <span
                  className="block h-full rounded-full bg-emerald-500/70 studio-transition"
                  style={{ width: `${Math.round((row.count / widest) * 100)}%` }}
                />
              </span>
              <span className="w-10 shrink-0 text-right text-[13px] font-semibold tabular-nums text-slate-200">
                {row.count}
              </span>
            </div>
          ))}
        </div>
      </div>

      {(summary?.errors || 0) > 0 && (
        <div className="rounded-2xl border border-amber-800/60 bg-amber-950/30 p-4 flex items-start gap-3">
          <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
          <div className="text-[13px] text-amber-200/90">
            <strong className="font-semibold">
              {summary?.errors} error{summary?.errors === 1 ? '' : 's'} logged
            </strong>
            {typeof summary?.errors_retryable === 'number' && (
              <span className="text-amber-200/70">
                {' '}
                — {summary.errors_retryable} may clear on a re-run.
              </span>
            )}
            <span className="block text-amber-200/60 mt-0.5">
              The Errors tab of your Sheet has the detail.
            </span>
          </div>
        </div>
      )}

      <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 flex items-start gap-3">
        <Info className="w-4 h-4 text-slate-500 shrink-0 mt-0.5" />
        <p className="text-[12px] text-slate-500 leading-relaxed">
          <strong className="text-slate-400 font-medium">What is not here.</strong>{' '}
          Replies and closes are counted from the Stage column you set on the
          Sheet, so they are as current as your last update rather than
          observed automatically. There is no chart over time: the Sheet
          stores each lead's stage now, not when it changed, so a trend line
          would be invented. Follow-up reminders are not tracked at all.
        </p>
      </div>
    </div>
  );
};

const Header: React.FC<{ sheetUrl?: string }> = ({ sheetUrl }) => (
  <div className="flex flex-wrap items-center justify-between gap-3">
    <div>
      <h1 className="text-xl font-semibold text-slate-100 tracking-tight">Results</h1>
      <p className="mt-1 text-[13px] text-slate-400">
        Counted from your Sheet. Nothing estimated.
      </p>
    </div>
    {sheetUrl && (
      <a
        href={sheetUrl}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium border border-slate-700 text-slate-300 hover:text-slate-100 hover:border-slate-600 studio-transition"
      >
        Open the Sheet <ExternalLink className="w-3.5 h-3.5" />
      </a>
    )}
  </div>
);

const Big: React.FC<{
  label: string;
  value: number;
  note?: string;
  muted?: boolean;
}> = ({ label, value, note, muted }) => (
  <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4">
    <div
      className={`text-3xl font-semibold tabular-nums leading-none ${
        muted ? 'text-slate-500' : 'text-slate-100'
      }`}
    >
      {value}
    </div>
    <div className="mt-2 text-[12px] font-medium text-slate-300">{label}</div>
    {note && <div className="text-[11px] text-slate-600 mt-0.5">{note}</div>}
  </div>
);
