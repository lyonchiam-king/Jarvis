import React, { useState } from 'react';
import {
  ArrowRight,
  Loader2,
  Play,
  Terminal,
  CheckCircle2,
  FileCode,
  Hammer,
  Send,
  Radio,
  Search,
  MinusCircle,
} from 'lucide-react';
import { Summary, RunLog } from '../types';
import { api } from '../api/client';
import { NavTab } from '../components/Sidebar';
import { StageKey } from '../components/StageNav';

interface TodayViewProps {
  summary: Summary | null;
  runLog: RunLog | null;
  onRefresh: () => void;
  setActiveTab: (tab: NavTab) => void;
  /** Deep-link into one step of the campaign rather than its first. */
  openStage: (stage: StageKey) => void;
}

/**
 * The screen someone opens in the morning.
 *
 * It answers three questions in order: what should I do first, what is
 * waiting on me, and how is this going. The old version led with a row of
 * pipeline counters and seven run buttons, which is the same information
 * arranged so that nothing is first.
 *
 * Every number here is read from the Sheet. Where the data does not exist
 * -- follow-ups being the honest example -- the tile says so instead of
 * showing a zero that would read as "none due today".
 */
export const TodayView: React.FC<TodayViewProps> = ({
  summary,
  runLog,
  onRefresh,
  setActiveTab,
  openStage,
}) => {
  const [limit, setLimit] = useState(10);
  const [starting, setStarting] = useState(false);

  const running = runLog?.running || null;

  const runFull = async () => {
    setStarting(true);
    await api.runBatch('full', limit);
    setStarting(false);
    onRefresh();
  };

  const greeting = () => {
    const h = new Date().getHours();
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  };

  const noSheet = !summary?.sheet_url;
  const toApprove = summary?.awaiting_approval || 0;
  const toBuild = summary?.approved_unbuilt || 0;
  const toSend = summary?.to_draft || 0;
  const toCheck = summary?.whatsapp_waiting || 0;
  const toResearch = summary?.to_research || 0;
  const total = summary?.total || 0;
  const replied = summary?.by_stage?.Replied || 0;

  /**
   * One recommendation, chosen in the order the pipeline actually runs --
   * the furthest-along work first, because a message drafted and unsent is
   * money already spent and a lead going cold.
   */
  const next: {
    title: string;
    body: string;
    cta: string;
    go: () => void;
  } = noSheet
    ? {
        title: 'Connect your Google Sheet',
        body: 'Everything this tool does is written to a Sheet in your own Drive. Nothing can run until there is one.',
        cta: 'Open setup',
        go: () => setActiveTab('settings'),
      }
    : toSend > 0
      ? {
          title: `Review and send ${toSend} message${toSend === 1 ? '' : 's'}`,
          body: 'These are written and waiting. You read each one before it goes -- nothing sends on its own.',
          cta: 'Review them',
          go: () => openStage('send'),
        }
      : toBuild > 0
        ? {
            title: `Build ${toBuild} approved site${toBuild === 1 ? '' : 's'}`,
            body: 'You approved these briefs. The page has to exist before anyone is messaged about it.',
            cta: 'Go to Build',
            go: () => openStage('build'),
          }
        : toApprove > 0
          ? {
              title: `Approve ${toApprove} brief${toApprove === 1 ? '' : 's'}`,
              body: 'Each one is a plan for a site. Approving is what lets it be built.',
              cta: 'Review briefs',
              go: () => openStage('brief'),
            }
          : toCheck > 0
            ? {
                title: `Check ${toCheck} lead${toCheck === 1 ? '' : 's'} for reachability`,
                body: 'Worth answering before anything is built for them -- building is the expensive part.',
                cta: 'Run the check',
                go: () => openStage('reachability'),
              }
            : toResearch > 0
              ? {
                  title: `Research ${toResearch} lead${toResearch === 1 ? '' : 's'}`,
                  body: 'Reads each business and works out what to say. Costs per lead, and stops at the limit you set.',
                  cta: 'Go to List',
                  go: () => openStage('list'),
                }
              : {
                  title: total ? 'Find more businesses' : 'Find your first businesses',
                  body: total
                    ? 'Everything in the list has been worked through. A new search adds more.'
                    : 'Discovery searches Google Places and builds your list. Nothing is contacted at this stage.',
                  cta: 'Go to List',
                  go: () => openStage('list'),
                };

  const tasks = [
    {
      id: 'task-send',
      label: 'Messages to review and send',
      count: toSend,
      icon: <Send className="w-4 h-4" />,
      go: () => openStage('send'),
    },
    {
      id: 'task-build',
      label: 'Approved sites to build',
      count: toBuild,
      icon: <Hammer className="w-4 h-4" />,
      go: () => openStage('build'),
    },
    {
      id: 'task-brief',
      label: 'Briefs waiting on your approval',
      count: toApprove,
      icon: <FileCode className="w-4 h-4" />,
      go: () => openStage('brief'),
    },
    {
      id: 'task-reach',
      label: 'Leads not yet checked for reachability',
      count: toCheck,
      icon: <Radio className="w-4 h-4" />,
      go: () => openStage('reachability'),
    },
    {
      id: 'task-research',
      label: 'Leads still to research',
      count: toResearch,
      icon: <Search className="w-4 h-4" />,
      go: () => openStage('list'),
    },
  ].filter((t) => t.count > 0);

  return (
    <div id="today-view" className="space-y-5">
      {/* Greeting + the one thing to do */}
      <section className="rounded-2xl border border-slate-800 bg-slate-900 overflow-hidden">
        <div className="p-5 sm:p-6">
          <p className="text-[13px] text-slate-500">{greeting()}</p>
          <h1 className="mt-1.5 text-xl sm:text-2xl font-semibold text-slate-100 tracking-tight">
            {next.title}
          </h1>
          <p className="mt-2 text-[13px] text-slate-400 leading-relaxed max-w-xl">
            {next.body}
          </p>
          <button
            id="today-primary-action"
            onClick={next.go}
            className="mt-4 inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-emerald-500 text-slate-950 text-sm font-semibold hover:bg-emerald-400 studio-transition"
          >
            {next.cta} <ArrowRight className="w-4 h-4" />
          </button>
        </div>
      </section>

      {/* Honest numbers */}
      <section className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <Metric
          label="Ready to contact"
          value={toSend}
          note="Message written, waiting for you"
        />
        <Metric
          label="Replies"
          value={replied}
          note="Counted from the Stage column you set"
        />
        {/* Not supported, and said so. The Sheet holds no due dates, so a
            zero here would read as "none due today" -- which would be a
            claim, not a count. */}
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
          <div className="flex items-center gap-1.5 text-slate-600">
            <MinusCircle className="w-4 h-4" />
            <span className="text-[13px] font-medium">Follow-ups due</span>
          </div>
          <p className="mt-2 text-[11px] text-slate-600 leading-relaxed">
            Not tracked. Nothing records when a reply is owed, so this would
            be a guess rather than a count.
          </p>
        </div>
      </section>

      {/* The queue */}
      <section className="rounded-2xl border border-slate-800 bg-slate-900">
        <div className="px-5 py-4 border-b border-slate-800 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-100">Waiting on you</h2>
          {tasks.length > 0 && (
            <span className="text-[11px] text-slate-500 tabular-nums">
              {tasks.length} thing{tasks.length === 1 ? '' : 's'}
            </span>
          )}
        </div>
        {tasks.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <CheckCircle2 className="w-7 h-7 mx-auto text-emerald-500/70" />
            <p className="mt-3 text-sm font-medium text-slate-200">
              Nothing is waiting on you
            </p>
            <p className="mt-1 text-[12px] text-slate-500">
              {total
                ? 'Every lead in the list has been dealt with. Find more when you are ready.'
                : 'Start by finding some businesses.'}
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-slate-800">
            {tasks.map((t) => (
              <li key={t.id}>
                <button
                  id={t.id}
                  onClick={t.go}
                  className="w-full flex items-center justify-between gap-3 px-5 py-3.5 text-left hover:bg-slate-800/50 studio-transition"
                >
                  <span className="flex items-center gap-3 min-w-0">
                    <span className="text-slate-500">{t.icon}</span>
                    <span className="text-[13px] text-slate-200 truncate">
                      {t.label}
                    </span>
                  </span>
                  <span className="flex items-center gap-2 shrink-0">
                    <span className="text-[12px] font-semibold tabular-nums px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300">
                      {t.count}
                    </span>
                    <ArrowRight className="w-4 h-4 text-slate-600" />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* The campaign, as a card */}
      <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">Your campaign</h2>
            <p className="text-[12px] text-slate-500 mt-0.5">
              {total
                ? `${total} business${total === 1 ? '' : 'es'} in the list`
                : 'No businesses yet'}
            </p>
          </div>
          <button
            id="today-open-campaign"
            onClick={() => setActiveTab('campaigns')}
            className="inline-flex items-center gap-1.5 text-[13px] font-medium text-emerald-400 hover:text-emerald-300 studio-transition"
          >
            Open <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>

        {total > 0 && (
          <div className="mt-4 flex items-center gap-1.5">
            {[
              { k: 'Found', n: total },
              { k: 'Researched', n: total - toResearch },
              { k: 'Built', n: summary?.by_stage?.['Site Built'] || 0 },
              { k: 'Sent', n: summary?.by_stage?.Sent || 0 },
            ].map((s) => (
              <div key={s.k} className="flex-1">
                <div className="h-1.5 rounded-full bg-slate-800 overflow-hidden">
                  <div
                    className="h-full bg-emerald-500/70 rounded-full studio-transition"
                    style={{ width: `${Math.round((s.n / Math.max(total, 1)) * 100)}%` }}
                  />
                </div>
                <div className="mt-1.5 text-[10px] uppercase tracking-wider text-slate-500">
                  {s.k}
                </div>
                <div className="text-[13px] font-semibold tabular-nums text-slate-200">
                  {s.n}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* The whole morning batch, kept on Today because that is when it is
            run. It stops before building and before sending: the two steps
            that cost the most and the one that reaches a real business. */}
        <div className="mt-5 pt-4 border-t border-slate-800 flex flex-wrap items-center gap-2.5">
          <button
            id="run-full-batch"
            onClick={runFull}
            disabled={!!running || starting || noSheet}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-slate-700 text-slate-200 text-[13px] font-medium hover:bg-slate-800/70 hover:border-slate-600 studio-transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {starting || running === 'full' ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Play className="w-4 h-4" />
            )}
            Run this morning's batch
          </button>
          <label className="flex items-center gap-2 text-[12px] text-slate-500">
            up to
            <input
              id="today-batch-limit"
              type="number"
              min={1}
              max={60}
              value={limit}
              onChange={(e) => setLimit(Math.max(1, Number(e.target.value) || 1))}
              className="w-16 px-2 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-100 text-sm"
            />
            leads
          </label>
          <span className="text-[11px] text-slate-600">
            Finds, researches, briefs and drafts. Stops before building or sending.
          </span>
        </div>
      </section>

      {/* Recent activity -- the run log, kept small and only when there is
          something in it. */}
      {runLog && runLog.lines?.length > 0 && (
        <section className="rounded-2xl border border-slate-800 bg-slate-900">
          <div className="px-5 py-3.5 border-b border-slate-800 flex items-center gap-2">
            <Terminal className="w-4 h-4 text-slate-600" />
            <h2 className="text-sm font-semibold text-slate-100">Recent activity</h2>
            {running && (
              <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] font-medium text-emerald-400">
                <Loader2 className="w-3 h-3 animate-spin" />
                {running} running
              </span>
            )}
          </div>
          <div className="px-5 py-3 max-h-48 overflow-y-auto">
            <ul className="space-y-1">
              {runLog.lines.slice(-12).map((line, i) => (
                <li
                  key={i}
                  className="text-[11.5px] font-mono text-slate-500 leading-relaxed break-words"
                >
                  {line}
                </li>
              ))}
            </ul>
          </div>
        </section>
      )}
    </div>
  );
};

const Metric: React.FC<{ label: string; value: number; note: string }> = ({
  label,
  value,
  note,
}) => (
  <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4">
    <div className="text-3xl font-semibold tabular-nums leading-none text-slate-100">
      {value}
    </div>
    <div className="mt-2 text-[13px] font-medium text-slate-300">{label}</div>
    <div className="text-[11px] text-slate-600 mt-0.5 leading-relaxed">{note}</div>
  </div>
);
