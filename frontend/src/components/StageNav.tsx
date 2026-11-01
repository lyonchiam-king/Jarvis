import React from 'react';
import { Check, Lock, Circle, Loader2 } from 'lucide-react';

/**
 * The pipeline, drawn as the sequence it is.
 *
 * These five were five separate sidebar destinations, which made a
 * progression look like a menu: nothing said that a brief cannot exist
 * before research, or why Build was empty. As a navigator each step can
 * show whether it is done, waiting on you, or has nothing in it yet -- and
 * every step stays clickable, because revisiting an earlier one is normal
 * work rather than going backwards.
 */
export type StageKey =
  | 'list'
  | 'reachability'
  | 'brief'
  | 'build'
  // The infographic branch. Shown only when the campaign asks for graphics:
  // a step that can never do anything reads as broken, and on the original
  // track there is nothing for it to hold.
  | 'visual'
  | 'send';

/**
 * `waiting` means a person has to act. `done` means nothing is outstanding
 * *and* something has been through. `empty` is the honest third state: no
 * work here yet, which is not the same as finished and must not be drawn as
 * a tick. `blocked` is reserved for a real blocker upstream.
 */
export type StageState = 'waiting' | 'done' | 'empty' | 'blocked' | 'running';

export interface StageInfo {
  key: StageKey;
  label: string;
  /** What this step does, in the user's words. */
  blurb: string;
  state: StageState;
  /** How many items are waiting on a person here. Shown only when > 0. */
  count: number;
  /** Said out loud when the step is blocked, so the reason is never hidden. */
  blockedReason?: string;
}

interface StageNavProps {
  stages: StageInfo[];
  active: StageKey;
  onSelect: (key: StageKey) => void;
}

const dot = (state: StageState) => {
  if (state === 'running') return <Loader2 className="w-3.5 h-3.5 animate-spin" />;
  if (state === 'done') return <Check className="w-3.5 h-3.5" />;
  if (state === 'blocked') return <Lock className="w-3 h-3" />;
  return <Circle className="w-2 h-2 fill-current" />;
};

export const StageNav: React.FC<StageNavProps> = ({ stages, active, onSelect }) => (
  <nav
    id="stage-nav"
    aria-label="Campaign stages"
    // Scrolls sideways on a phone rather than wrapping: five steps stacked
    // into two rows stop reading as a sequence, which is the one thing this
    // component exists to convey.
    className="relative flex gap-2 overflow-x-auto pb-1 -mx-1 px-1"
  >
    {stages.map((stage, i) => {
      const isActive = stage.key === active;
      const tone =
        stage.state === 'waiting'
          ? 'text-emerald-300'
          : stage.state === 'done'
            ? 'text-emerald-500'
            : stage.state === 'blocked'
              ? 'text-amber-500'
              : 'text-slate-500';

      return (
        <React.Fragment key={stage.key}>
          <button
            id={`stage-${stage.key}`}
            onClick={() => onSelect(stage.key)}
            aria-current={isActive ? 'step' : undefined}
            title={stage.blockedReason || stage.blurb}
            className={`group shrink-0 min-w-[9.5rem] flex-1 text-left px-3.5 py-3 rounded-xl border studio-transition ${
              isActive
                ? 'bg-emerald-500/12 border-emerald-500/40'
                : 'bg-slate-900 border-slate-800 hover:border-slate-700 hover:bg-slate-800/60'
            }`}
          >
            <span className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 min-w-0">
                <span className={`shrink-0 ${tone}`}>{dot(stage.state)}</span>
                <span
                  className={`text-[13px] font-medium truncate ${
                    isActive ? 'text-emerald-200' : 'text-slate-200'
                  }`}
                >
                  {stage.label}
                </span>
              </span>
              {stage.count > 0 && (
                <span className="shrink-0 text-[11px] font-semibold tabular-nums px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300">
                  {stage.count}
                </span>
              )}
            </span>
            <span className="mt-1 block text-[11px] text-slate-500 leading-snug">
              {stage.state === 'blocked'
                ? stage.blockedReason
                : stage.count > 0
                  ? `${stage.count} waiting`
                  : stage.state === 'done'
                    ? 'Nothing outstanding'
                    : 'Nothing here yet'}
            </span>
          </button>
          {i < stages.length - 1 && (
            <span
              aria-hidden="true"
              className="hidden xl:block self-center w-3 h-px bg-slate-800 shrink-0"
            />
          )}
        </React.Fragment>
      );
    })}
  </nav>
);
