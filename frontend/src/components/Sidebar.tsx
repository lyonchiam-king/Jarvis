import React from 'react';
import {
  LayoutDashboard,
  Compass,
  Users,
  BarChart3,
  Settings,
  CheckCircle2,
  AlertCircle,
  Radio,
  Bot,
} from 'lucide-react';
import { Summary, WhatsAppStatus, ModelSettings } from '../types';

/**
 * Four places to be, and Settings.
 *
 * The old sidebar listed every stage of the pipeline as a separate
 * destination -- Leads, Reachability, Briefs, Build, Send -- which turned a
 * sequence into a menu and left the reader to work out the order for
 * themselves. The stages are a progression, so they now live inside
 * Campaigns as a progress navigator; this list answers "where am I
 * working", not "what happens next".
 */
export type NavTab = 'today' | 'campaigns' | 'contacts' | 'results' | 'settings';

interface SidebarProps {
  activeTab: NavTab;
  setActiveTab: (tab: NavTab) => void;
  summary: Summary | null;
  whatsappStatus: WhatsAppStatus | null;
  modelSettings: ModelSettings | null;
  onOpenOnboarding: () => void;
  /** Open on phones, where the sidebar is a drawer rather than a column. */
  navOpen: boolean;
  onNavClose: () => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  activeTab,
  setActiveTab,
  summary,
  whatsappStatus,
  modelSettings,
  onOpenOnboarding,
  navOpen,
  onNavClose,
}) => {
  // The badge on Campaigns is everything waiting on a person: briefs to
  // approve, approved sites not yet built, messages drafted and unsent.
  // One number, because the point of it is "there is work in here".
  const waiting =
    (summary?.awaiting_approval || 0) +
    (summary?.approved_unbuilt || 0) +
    (summary?.to_draft || 0);

  const navItems: {
    id: NavTab;
    label: string;
    hint: string;
    icon: React.ReactNode;
    badge?: number;
  }[] = [
    {
      id: 'today',
      label: 'Today',
      hint: 'What needs you now',
      icon: <LayoutDashboard className="w-[18px] h-[18px]" />,
    },
    {
      id: 'campaigns',
      label: 'Campaigns',
      hint: 'Work the pipeline',
      icon: <Compass className="w-[18px] h-[18px]" />,
      badge: waiting,
    },
    {
      id: 'contacts',
      label: 'Contacts',
      hint: 'Every business found',
      icon: <Users className="w-[18px] h-[18px]" />,
      badge: summary?.total,
    },
    {
      id: 'results',
      label: 'Results',
      hint: 'What came of it',
      icon: <BarChart3 className="w-[18px] h-[18px]" />,
    },
  ];

  const sheetOk = !!summary?.sheet_url;
  const keysOk = !!modelSettings?.key_set;
  const relayOk = !!whatsappStatus?.connected;

  const row = (label: string, ok: boolean, okWord: string, badWord: string) => (
    <div className="flex items-center justify-between">
      <span className="text-slate-400">{label}</span>
      {ok ? (
        <span className="flex items-center gap-1 text-emerald-400 font-medium text-[11px]">
          <CheckCircle2 className="w-3 h-3" /> {okWord}
        </span>
      ) : (
        <span className="flex items-center gap-1 text-amber-500 font-medium text-[11px]">
          <AlertCircle className="w-3 h-3" /> {badWord}
        </span>
      )}
    </div>
  );

  return (
    <>
      {/* Tapping away closes the drawer. Phones only: on a wide screen the
          sidebar is part of the layout and there is nothing to dismiss. */}
      {navOpen && (
        <div
          id="sidebar-scrim"
          onClick={onNavClose}
          className="fixed inset-0 z-40 bg-slate-950/70 backdrop-blur-xs lg:hidden"
        />
      )}
      <aside
        id="app-sidebar"
        className={`w-[264px] border-r flex flex-col shrink-0 studio-transition
          fixed inset-y-0 left-0 z-50 overflow-y-auto
          lg:static lg:z-auto lg:translate-x-0
          ${navOpen ? 'translate-x-0' : '-translate-x-full'}
          bg-slate-950 border-slate-800 text-slate-100`}
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        {/* Brand */}
        <div id="sidebar-header" className="px-5 pt-6 pb-5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-emerald-500 text-slate-950 flex items-center justify-center font-bold text-lg">
              O
            </div>
            <div>
              <h1 className="font-semibold text-[15px] leading-tight tracking-tight text-slate-100">
                Outreach Studio
              </h1>
              <p className="text-[11px] text-slate-500">SME outreach pipeline</p>
            </div>
          </div>
        </div>

        {/* Where to be */}
        <nav id="sidebar-nav" className="flex-1 px-3 space-y-1">
          {navItems.map((item) => {
            const isActive = activeTab === item.id;
            return (
              <button
                key={item.id}
                id={`nav-item-${item.id}`}
                aria-current={isActive ? 'page' : undefined}
                onClick={() => {
                  setActiveTab(item.id);
                  // On a phone the drawer covers the screen it just
                  // navigated to, so picking a tab has to close it.
                  onNavClose();
                }}
                className={`w-full flex items-center justify-between gap-3 px-3 py-2.5 rounded-xl text-left studio-transition ${
                  isActive
                    ? 'bg-emerald-500/12 text-emerald-300 ring-1 ring-emerald-500/25'
                    : 'text-slate-300 hover:bg-slate-800/70 hover:text-slate-100'
                }`}
              >
                <span className="flex items-center gap-3 min-w-0">
                  <span className={isActive ? 'text-emerald-400' : 'text-slate-500'}>
                    {item.icon}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm font-medium leading-tight">
                      {item.label}
                    </span>
                    <span className="block text-[11px] text-slate-500 leading-tight truncate">
                      {item.hint}
                    </span>
                  </span>
                </span>
                {typeof item.badge === 'number' && item.badge > 0 && (
                  <span
                    className={`shrink-0 text-[11px] px-2 py-0.5 rounded-full font-semibold tabular-nums ${
                      isActive
                        ? 'bg-emerald-500/20 text-emerald-200'
                        : 'bg-slate-800 text-slate-400'
                    }`}
                  >
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        {/* Setup health. Kept because it answers "why is nothing happening",
            which is otherwise a hunt through three different screens. */}
        <div
          id="sidebar-setup-box"
          className="mx-3 mb-3 mt-4 p-3 rounded-xl border bg-slate-900/80 border-slate-800"
        >
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
              Setup
            </span>
            <button
              id="setup-checklist-link"
              onClick={onOpenOnboarding}
              className="text-[11px] font-medium text-emerald-400 hover:text-emerald-300 studio-transition"
            >
              Checklist
            </button>
          </div>
          <div className="space-y-1.5 text-xs">
            {row('Google Sheet', sheetOk, 'Connected', 'Needed')}
            {row('Model key', keysOk, 'Set', 'Needed')}
            <div className="flex items-center justify-between">
              <span className="text-slate-400">WhatsApp relay</span>
              {relayOk ? (
                <span className="flex items-center gap-1 text-emerald-400 font-medium text-[11px]">
                  <CheckCircle2 className="w-3 h-3" /> Connected
                </span>
              ) : (
                // Not an error. The relay is optional -- without it messages
                // are sent by hand from the phone, which is the whole point
                // of the click-to-chat link.
                <span className="flex items-center gap-1 text-slate-500 text-[11px]">
                  <Radio className="w-3 h-3" /> Off
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Settings, deliberately quieter than the four above it. */}
        <div className="px-3 pb-2">
          <button
            id="nav-item-settings"
            aria-current={activeTab === 'settings' ? 'page' : undefined}
            onClick={() => {
              setActiveTab('settings');
              onNavClose();
            }}
            className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm studio-transition ${
              activeTab === 'settings'
                ? 'bg-slate-800 text-slate-100'
                : 'text-slate-500 hover:text-slate-300 hover:bg-slate-800/60'
            }`}
          >
            <Settings className="w-4 h-4" />
            Settings
          </button>
        </div>

        <div
          id="sidebar-footer"
          className="px-5 py-4 border-t border-slate-800/80 text-xs text-slate-500 space-y-2"
        >
          <div className="flex items-center gap-2 flex-wrap">
            <Bot className="w-3.5 h-3.5 text-emerald-500" />
            <span className="font-medium text-slate-400">
              {modelSettings?.provider ? modelSettings.provider.toUpperCase() : 'NO PROVIDER'}
            </span>
            <span className="text-[10px] bg-slate-800 px-1.5 py-0.5 rounded text-slate-400">
              {modelSettings?.model || 'not set'}
            </span>
          </div>
          <p className="text-[11px] text-slate-600 leading-snug">
            Runs on your own machine. The Sheet and WhatsApp stay in your
            private environment.
          </p>
        </div>
      </aside>
    </>
  );
};
