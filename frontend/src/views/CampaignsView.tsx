import React, { useState, useEffect } from 'react';
import {
  Play,
  Loader2,
  SlidersHorizontal,
  MapPin,
  Search,
  ArrowRight,
  Inbox,
} from 'lucide-react';
import { StageNav, StageKey, StageInfo, StageState } from '../components/StageNav';
import { ReachabilityView } from './ReachabilityView';
import { BriefsQueueView } from './BriefsQueueView';
import { BuildQueueView } from './BuildQueueView';
import { SendQueueView } from './SendQueueView';
import { CampaignView } from './CampaignView';
import { Summary, WhatsAppStatus, CampaignConfig, RunLog } from '../types';
import { api } from '../api/client';

interface CampaignsViewProps {
  summary: Summary | null;
  whatsappStatus: WhatsAppStatus | null;
  runLog: RunLog | null;
  onRefresh: () => void;
  /** Which step to land on. Today's cards deep-link into a stage. */
  initialStage?: StageKey;
}

/**
 * One campaign, worked from left to right.
 *
 * Everything inside was already built and already works; what was missing
 * was any sense that the five screens were one sequence, or of where the
 * campaign had got to. Nothing here changes what the stages do -- the
 * queue views are the same components, with the same confirmations before
 * anything reaches a real business.
 */
export const CampaignsView: React.FC<CampaignsViewProps> = ({
  summary,
  whatsappStatus,
  runLog,
  onRefresh,
  initialStage,
}) => {
  const [stage, setStage] = useState<StageKey>(initialStage || 'list');
  const [showSettings, setShowSettings] = useState(false);
  const [campaign, setCampaign] = useState<CampaignConfig | null>(null);

  useEffect(() => {
    if (initialStage) setStage(initialStage);
  }, [initialStage]);

  useEffect(() => {
    api.getCampaign().then((res) => {
      if (res.data) setCampaign(res.data);
    });
  }, []);

  // Nothing can happen without a Sheet, so that is the one thing allowed to
  // mark every step blocked. Any other emptiness is just emptiness.
  const noSheet = !summary?.sheet_url;
  const anythingAtAll = (summary?.total || 0) > 0;

  const describe = (count: number, reached: boolean): StageState => {
    if (noSheet) return 'blocked';
    if (count > 0) return 'waiting';
    return reached ? 'done' : 'empty';
  };

  const by = summary?.by_stage;
  const built = by?.['Site Built'] || 0;
  const sent = by?.Sent || 0;

  const stages: StageInfo[] = [
    {
      key: 'list',
      label: 'List',
      blurb: 'Find businesses worth approaching',
      state: noSheet ? 'blocked' : anythingAtAll ? 'done' : 'empty',
      count: 0,
      blockedReason: noSheet ? 'Connect a Google Sheet first' : undefined,
    },
    {
      key: 'reachability',
      label: 'Reachability',
      blurb: 'Check who can actually be contacted',
      state: describe(summary?.whatsapp_waiting || 0, anythingAtAll),
      count: summary?.whatsapp_waiting || 0,
      blockedReason: noSheet ? 'Connect a Google Sheet first' : undefined,
    },
    {
      key: 'brief',
      label: 'Brief',
      blurb: 'Approve what gets built for each one',
      state: describe(summary?.awaiting_approval || 0, anythingAtAll),
      count: summary?.awaiting_approval || 0,
      blockedReason: noSheet ? 'Connect a Google Sheet first' : undefined,
    },
    {
      key: 'build',
      label: 'Build',
      blurb: 'Turn approved briefs into real pages',
      state: describe(summary?.approved_unbuilt || 0, built > 0),
      count: summary?.approved_unbuilt || 0,
      blockedReason: noSheet ? 'Connect a Google Sheet first' : undefined,
    },
    {
      key: 'send',
      label: 'Review & Send',
      blurb: 'Read every message before it goes',
      state: describe(summary?.to_draft || 0, sent > 0),
      count: summary?.to_draft || 0,
      blockedReason: noSheet ? 'Connect a Google Sheet first' : undefined,
    },
  ];

  return (
    <div id="campaigns-view" className="space-y-5">
      {/* Campaign header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-slate-100 tracking-tight">
            {campaign?.category
              ? campaign.category.replace(/^./, (c) => c.toUpperCase())
              : 'Your campaign'}
          </h1>
          <p className="mt-1 text-[13px] text-slate-400 flex items-center gap-1.5">
            <MapPin className="w-3.5 h-3.5 text-slate-500" />
            {campaign?.location || 'Location not set'}
            {campaign?.areas?.length ? (
              <span className="text-slate-600">
                · {campaign.areas.length} area{campaign.areas.length === 1 ? '' : 's'}
              </span>
            ) : null}
          </p>
        </div>
        <button
          id="campaign-settings-toggle"
          onClick={() => setShowSettings((v) => !v)}
          aria-expanded={showSettings}
          className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg text-[13px] font-medium border border-slate-700 text-slate-300 hover:text-slate-100 hover:border-slate-600 hover:bg-slate-800/60 studio-transition"
        >
          <SlidersHorizontal className="w-4 h-4" />
          {showSettings ? 'Hide settings' : 'Campaign settings'}
        </button>
      </div>

      {showSettings && (
        <div
          id="campaign-settings-panel"
          className="rounded-2xl border border-slate-800 bg-slate-900/60 p-1"
        >
          <CampaignView />
        </div>
      )}

      <StageNav stages={stages} active={stage} onSelect={setStage} />

      <div id="stage-panel">
        {stage === 'list' && (
          <ListStage summary={summary} runLog={runLog} onRefresh={onRefresh} />
        )}
        {stage === 'reachability' && <ReachabilityView />}
        {stage === 'brief' && <BriefsQueueView />}
        {stage === 'build' && <BuildQueueView />}
        {stage === 'send' && (
          <SendQueueView whatsappStatus={whatsappStatus} onRefresh={onRefresh} />
        )}
      </div>
    </div>
  );
};

/**
 * The first stage: building the list.
 *
 * Discovery and research were only reachable from the Today screen's run
 * buttons, which put the start of the pipeline outside the pipeline. The
 * preview is here for the same reason it exists at all -- a Places search
 * costs money, and seeing who would be picked before spending is cheaper
 * than regretting it.
 */
const ListStage: React.FC<{
  summary: Summary | null;
  runLog: RunLog | null;
  onRefresh: () => void;
}> = ({ summary, runLog, onRefresh }) => {
  const [limit, setLimit] = useState(10);
  const [starting, setStarting] = useState<string | null>(null);
  const [preview, setPreview] = useState<string[] | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const running = runLog?.running || null;

  const run = async (what: string) => {
    setStarting(what);
    await api.runBatch(what, limit);
    setStarting(null);
    onRefresh();
  };

  const showPreview = async () => {
    setPreviewing(true);
    const res = await api.getNextUp();
    setPreview(res.data?.names || []);
    setPreviewing(false);
  };

  const action = (
    id: string,
    label: string,
    note: string,
    what: string,
    primary: boolean,
  ) => (
    <button
      id={id}
      onClick={() => run(what)}
      disabled={!!running || !!starting}
      className={`flex-1 min-w-[11rem] text-left px-4 py-3 rounded-xl studio-transition disabled:opacity-50 disabled:cursor-not-allowed ${
        primary
          ? 'bg-emerald-500 text-slate-950 hover:bg-emerald-400 font-semibold'
          : 'border border-slate-700 text-slate-200 hover:bg-slate-800/70 hover:border-slate-600'
      }`}
    >
      <span className="flex items-center gap-2 text-sm font-medium">
        {starting === what || running === what ? (
          <Loader2 className="w-4 h-4 animate-spin" />
        ) : (
          <Play className="w-4 h-4" />
        )}
        {label}
      </span>
      <span
        className={`mt-0.5 block text-[11px] ${
          primary ? 'text-emerald-950/70' : 'text-slate-500'
        }`}
      >
        {note}
      </span>
    </button>
  );

  return (
    <div className="space-y-4">
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">Build the list</h2>
            <p className="text-[12px] text-slate-500 mt-0.5">
              Finding and researching leads costs money per lead. Both stop at
              the number below.
            </p>
          </div>
          <label className="flex items-center gap-2 text-[12px] text-slate-400">
            How many
            <input
              id="list-limit"
              type="number"
              min={1}
              max={60}
              value={limit}
              onChange={(e) => setLimit(Math.max(1, Number(e.target.value) || 1))}
              className="w-20 px-2.5 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-100 text-sm"
            />
          </label>
        </div>

        <div className="flex flex-wrap gap-2.5">
          {action('run-discovery', 'Find new leads', 'Searches Google Places', 'discovery', true)}
          {action('run-research', 'Research them', 'Reads each one, then checks reachability', 'research', false)}
          <button
            id="preview-next-up"
            onClick={showPreview}
            disabled={previewing}
            className="flex-1 min-w-[11rem] text-left px-4 py-3 rounded-xl border border-slate-700 text-slate-200 hover:bg-slate-800/70 hover:border-slate-600 studio-transition disabled:opacity-50"
          >
            <span className="flex items-center gap-2 text-sm font-medium">
              {previewing ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Search className="w-4 h-4" />
              )}
              Who is next
            </span>
            <span className="mt-0.5 block text-[11px] text-slate-500">
              Writes nothing, spends nothing
            </span>
          </button>
        </div>

        {preview !== null && (
          <div id="next-up-list" className="mt-4 pt-4 border-t border-slate-800">
            {preview.length === 0 ? (
              <p className="text-[12px] text-slate-500">
                Nobody is queued for research. Find new leads first.
              </p>
            ) : (
              <>
                <p className="text-[11px] uppercase tracking-wider text-slate-500 mb-2">
                  Next up
                </p>
                <ul className="space-y-1">
                  {preview.map((name, i) => (
                    <li
                      key={`${name}-${i}`}
                      className="text-[13px] text-slate-300 flex items-center gap-2"
                    >
                      <span className="text-slate-600 tabular-nums text-[11px] w-5">
                        {i + 1}
                      </span>
                      {name}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </div>

      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5">
        <h2 className="text-sm font-semibold text-slate-100 mb-3">In the list</h2>
        {summary?.total ? (
          <div className="flex flex-wrap gap-6">
            <Stat label="Businesses found" value={summary.total} />
            <Stat label="Researched" value={summary.total - (summary.to_research || 0)} />
            <Stat label="Still to research" value={summary.to_research || 0} />
            <Stat label="Disqualified" value={summary.disqualified || 0} muted />
          </div>
        ) : (
          <Empty
            title="No businesses yet"
            body="Press Find new leads above. Nothing is contacted at this stage -- discovery only builds a list."
          />
        )}
      </div>
    </div>
  );
};

const Stat: React.FC<{ label: string; value: number; muted?: boolean }> = ({
  label,
  value,
  muted,
}) => (
  <div>
    <div
      className={`text-2xl font-semibold tabular-nums ${
        muted ? 'text-slate-500' : 'text-slate-100'
      }`}
    >
      {value}
    </div>
    <div className="text-[11px] uppercase tracking-wider text-slate-500 mt-0.5">
      {label}
    </div>
  </div>
);

export const Empty: React.FC<{ title: string; body: string; action?: React.ReactNode }> = ({
  title,
  body,
  action,
}) => (
  <div className="py-10 px-4 text-center">
    <Inbox className="w-7 h-7 mx-auto text-slate-700" />
    <p className="mt-3 text-sm font-medium text-slate-300">{title}</p>
    <p className="mt-1 text-[12px] text-slate-500 max-w-sm mx-auto leading-relaxed">
      {body}
    </p>
    {action && <div className="mt-4 flex justify-center">{action}</div>}
  </div>
);

export const Nudge: React.FC<{ label: string; onClick: () => void }> = ({
  label,
  onClick,
}) => (
  <button
    onClick={onClick}
    className="inline-flex items-center gap-1.5 text-[13px] font-medium text-emerald-400 hover:text-emerald-300 studio-transition"
  >
    {label} <ArrowRight className="w-3.5 h-3.5" />
  </button>
);
