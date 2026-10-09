import React, { useState, useEffect, useCallback } from 'react';
import { Sidebar, NavTab } from './components/Sidebar';
import { OnboardingBanner } from './components/OnboardingBanner';
import { StageKey } from './components/StageNav';
import { TodayView } from './views/TodayView';
import { CampaignsView } from './views/CampaignsView';
import { LeadsView } from './views/LeadsView';
import { ResultsView } from './views/ResultsView';
import { SettingsView } from './views/SettingsView';
import { Summary, RunLog, WhatsAppStatus, ModelSettings, Lead } from './types';
import { api } from './api/client';
import { ExternalLink, Sparkles, CheckCircle2, Menu } from 'lucide-react';

const TITLES: Record<NavTab, string> = {
  today: 'Today',
  campaigns: 'Campaigns',
  contacts: 'Contacts',
  results: 'Results',
  settings: 'Settings',
};

export function App() {
  const [activeTab, setActiveTab] = useState<NavTab>('today');
  const [onboardingOpen, setOnboardingOpen] = useState(true);
  const [navOpen, setNavOpen] = useState(false);
  // Which step of the campaign to land on when Today deep-links into one.
  const [stage, setStage] = useState<StageKey | undefined>(undefined);

  // Core Data State
  const [summary, setSummary] = useState<Summary | null>(null);
  const [runLog, setRunLog] = useState<RunLog | null>(null);
  const [whatsappStatus, setWhatsappStatus] = useState<WhatsAppStatus | null>(null);
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);

  const fetchCoreData = useCallback(async () => {
    const [sRes, lRes, wRes, mRes, leadsRes] = await Promise.all([
      api.getSummary(),
      api.getLog(),
      api.getWhatsAppStatus(),
      api.getModel(),
      api.getLeads(),
    ]);

    if (sRes.data) setSummary(sRes.data);
    if (lRes.data) setRunLog(lRes.data);
    if (wRes.data) setWhatsappStatus(wRes.data);
    if (mRes.data) setModelSettings(mRes.data);
    if (leadsRes.data) {
      const leadsArray = Array.isArray(leadsRes.data)
        ? leadsRes.data
        : leadsRes.data.leads || [];
      setLeads(leadsArray);
    }
  }, []);

  useEffect(() => {
    fetchCoreData();
  }, [fetchCoreData]);

  // Polling for live logs if a batch is running
  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | null = null;
    if (runLog?.running) {
      interval = setInterval(fetchCoreData, 3000);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [runLog?.running, fetchCoreData]);

  /*
   * The workspace is dark, full stop -- the palette in index.css is built
   * for one ground and reads badly inverted. Every view still carries its
   * `dark:` variants, so the class stays on the root rather than the
   * variants being stripped out of three thousand lines by hand.
   */
  useEffect(() => {
    document.documentElement.classList.add('dark');
  }, []);

  // Today's cards open a specific step. Anything else that reaches
  // Campaigns lands on whichever step the view itself decides.
  const openStage = (next: StageKey) => {
    setStage(next);
    setActiveTab('campaigns');
  };

  return (
    <div
      id="app-root"
      className="min-h-screen flex font-sans antialiased bg-slate-900 text-slate-100"
    >
      <Sidebar
        activeTab={activeTab}
        setActiveTab={(tab) => {
          // Reaching Campaigns from the sidebar should not silently reopen
          // the step a card sent you to twenty minutes ago.
          if (tab === 'campaigns') setStage(undefined);
          setActiveTab(tab);
        }}
        summary={summary}
        whatsappStatus={whatsappStatus}
        modelSettings={modelSettings}
        onOpenOnboarding={() => setOnboardingOpen(true)}
        navOpen={navOpen}
        onNavClose={() => setNavOpen(false)}
      />

      <main id="main-content" className="flex-1 flex flex-col min-w-0 overflow-y-auto">
        <header
          id="top-app-header"
          style={{ paddingTop: 'calc(0.875rem + env(safe-area-inset-top))' }}
          className="sticky top-0 z-30 px-4 sm:px-6 py-3.5 border-b bg-slate-900/85 backdrop-blur-md border-slate-800 flex items-center justify-between gap-3"
        >
          <div className="flex items-center gap-3 min-w-0">
            <button
              id="nav-toggle-btn"
              onClick={() => setNavOpen(true)}
              aria-label="Open menu"
              className="lg:hidden -ml-1 p-2 rounded-lg text-slate-400 hover:text-slate-100 hover:bg-slate-800 studio-transition"
            >
              <Menu className="w-5 h-5" />
            </button>
            <span className="font-semibold text-sm tracking-tight text-slate-200 truncate">
              {TITLES[activeTab]}
            </span>

            {summary?.sheet_url && (
              <a
                href={summary.sheet_url}
                target="_blank"
                rel="noreferrer"
                className="hidden sm:inline-flex items-center gap-1.5 px-2.5 py-1 bg-emerald-500/10 text-emerald-300 border border-emerald-500/25 rounded-full text-[11px] font-medium hover:bg-emerald-500/15 studio-transition"
              >
                <CheckCircle2 className="w-3 h-3" /> Sheet connected{' '}
                <ExternalLink className="w-2.5 h-2.5" />
              </a>
            )}
          </div>

          <button
            id="quick-start-toggle-btn"
            onClick={() => setOnboardingOpen(!onboardingOpen)}
            className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 border border-slate-700 text-slate-300 rounded-lg text-[12px] font-medium hover:bg-slate-800 hover:text-slate-100 studio-transition"
          >
            <Sparkles className="w-3.5 h-3.5 text-emerald-400" />
            <span className="hidden sm:inline">
              {onboardingOpen ? 'Hide setup' : 'Setup checklist'}
            </span>
            <span className="sm:hidden">Setup</span>
          </button>
        </header>

        <div
          className="p-4 sm:p-6 lg:p-8 flex-1 max-w-6xl w-full mx-auto space-y-6"
          style={{ paddingBottom: 'calc(2rem + env(safe-area-inset-bottom))' }}
        >
          <OnboardingBanner
            summary={summary}
            modelSettings={modelSettings}
            whatsappStatus={whatsappStatus}
            onRefresh={fetchCoreData}
            isOpen={onboardingOpen}
            onClose={() => setOnboardingOpen(false)}
          />

          {activeTab === 'today' && (
            <TodayView
              summary={summary}
              runLog={runLog}
              onRefresh={fetchCoreData}
              setActiveTab={setActiveTab}
              openStage={openStage}
            />
          )}

          {activeTab === 'campaigns' && (
            <CampaignsView
              summary={summary}
              whatsappStatus={whatsappStatus}
              runLog={runLog}
              onRefresh={fetchCoreData}
              initialStage={stage}
            />
          )}

          {activeTab === 'contacts' && (
            <LeadsView leads={leads} onRefresh={fetchCoreData} />
          )}

          {activeTab === 'results' && <ResultsView summary={summary} />}

          {activeTab === 'settings' && <SettingsView />}
        </div>
      </main>
    </div>
  );
}

export default App;
