import React, { useState, useEffect } from 'react';
import {
  Settings,
  Key,
  ShieldCheck,
  Server,
  Lock,
  ExternalLink,
  CheckCircle2,
  AlertCircle,
  Smartphone,
  Save,
  Info,
} from 'lucide-react';
import { ModelSettings, WhatsAppStatus } from '../types';
import { api } from '../api/client';

export const SettingsView: React.FC = () => {
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null);
  const [whatsappStatus, setWhatsappStatus] = useState<WhatsAppStatus | null>(null);
  const [loading, setLoading] = useState(true);

  // Inputs
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  // The model that writes every message -- the website ones and the
  // infographic ones alike. Read by the client since the beginning and
  // rendered by nothing, so it could only be changed from the other page.
  const [outreachModel, setOutreachModel] = useState('');
  const [catalogue, setCatalogue] = useState<string[] | null>(null);
  const [catalogueMsg, setCatalogueMsg] = useState<string | null>(null);
  const [catalogueLoading, setCatalogueLoading] = useState(false);
  const [resetArmed, setResetArmed] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [resetNote, setResetNote] = useState<string | null>(null);
  const [placesKeyInput, setPlacesKeyInput] = useState('');
  const [modelKeyInput, setModelKeyInput] = useState('');

  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  const [testPlacesLoading, setTestPlacesLoading] = useState(false);
  const [testModelLoading, setTestModelLoading] = useState(false);
  const [placesTestMsg, setPlacesTestMsg] = useState<string | null>(null);
  const [modelTestMsg, setModelTestMsg] = useState<string | null>(null);

  const loadData = async () => {
    setLoading(true);
    const [mRes, wRes] = await Promise.all([api.getModel(), api.getWhatsAppStatus()]);
    if (mRes.data) {
      setModelSettings(mRes.data);
      setProvider(mRes.data.provider);
      setModel(mRes.data.model);
      setOutreachModel(mRes.data.outreach_model);
    }
    if (wRes.data) {
      setWhatsappStatus(wRes.data);
    }
    setLoading(false);
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleSaveModelSettings = async () => {
    setSaving(true);
    setSaveMsg(null);
    const res = await api.saveModel({
      provider,
      model,
      // Sent explicitly. Blank is a real value here -- it means "use the
      // model above" -- so it cannot be omitted the way an absent field is.
      outreach_model: outreachModel,
      places_key: placesKeyInput || undefined,
      api_key: modelKeyInput || undefined,
    });
    setSaving(false);
    setPlacesKeyInput('');
    setModelKeyInput('');
    // A save that failed used to clear the boxes and say "Saved", so a
    // rejected provider name looked like a successful change.
    setSaveMsg(res.error ? `Not saved -- ${res.error}` : 'Saved.');
    setTimeout(() => setSaveMsg(null), 6000);
    loadData();
  };

  const loadCatalogue = async () => {
    setCatalogueLoading(true);
    setCatalogue(null);
    setCatalogueMsg(null);
    const res = await api.getModelCatalogue();
    setCatalogueLoading(false);
    if (res.error) {
      setCatalogueMsg(res.error);
      return;
    }
    const names = res.data?.models || [];
    setCatalogue(names);
    if (!names.length) {
      setCatalogueMsg(
        'Your key sees no models at all. That usually means it belongs to a ' +
          'different provider than the one selected above.'
      );
    }
  };

  const handleTestKey = async (type: 'places' | 'model') => {
    if (type === 'places') {
      setTestPlacesLoading(true);
      setPlacesTestMsg(null);
      const res = await api.testKey('places', placesKeyInput);
      setTestPlacesLoading(false);
      setPlacesTestMsg(res.data?.message || res.error || 'Test failed');
    } else {
      setTestModelLoading(true);
      setModelTestMsg(null);
        const res = await api.testKey('model', modelKeyInput, { provider, model });
      setTestModelLoading(false);
      setModelTestMsg(res.data?.message || res.error || 'Test failed');
    }
  };

  if (loading) {
    return (
      <div id="settings-loading" className="p-8 text-center text-slate-500 animate-pulse">
        Loading configuration settings...
      </div>
    );
  }

  return (
    <div id="settings-view" className="space-y-6 max-w-3xl mx-auto">
      {/*
        The way back to the original dashboard, named in the place somebody
        goes looking to switch between them.

        It is already in the sidebar footer, and was still asked for -- "why
        can't I go back to the standard dashboard?". A link that has to be
        hunted for is the link's fault, and the sidebar footer sits below the
        setup box and Settings, so on a short window it needs the sidebar
        itself scrolled. Saying the address out loud matters as much as the
        button: it is the one way back that works even when this page will
        not render at all, which is the moment it is most wanted.
      */}
      <div
        id="settings-classic"
        className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs flex flex-col sm:flex-row sm:items-center justify-between gap-3"
      >
        <div>
          <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider">
            The original dashboard
          </h2>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
            Still here, unchanged, with every button it ever had. Its
            address is{' '}
            <code className="text-slate-400">
              {typeof window === 'undefined'
                ? '/classic'
                : `${window.location.origin}/classic`}
            </code>
            . Worth a bookmark: it is the way back even if this page will not
            load.
          </p>
        </div>
        <a
          id="settings-classic-link"
          href="/classic"
          className="shrink-0 px-4 py-2 rounded-lg border border-slate-300 dark:border-slate-700 text-xs font-semibold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 studio-transition text-center"
        >
          Open the original dashboard
        </a>
      </div>

      {/* Header */}
      <div className="flex items-center justify-between p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs">
        <div>
          <h1 className="text-lg font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
            <Settings className="w-5 h-5 text-teal-600" /> Environment Settings &amp; Security
          </h1>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
            Configure LLM providers, Google Places API credentials, and WhatsApp relay endpoint.
          </p>
        </div>

        <button
          id="save-settings-btn"
          onClick={handleSaveModelSettings}
          disabled={saving}
          className="inline-flex items-center gap-1.5 px-4 py-2 bg-teal-600 hover:bg-teal-500 text-white font-bold text-xs rounded-md shadow-xs transition"
        >
          <Save className="w-3.5 h-3.5" />
          {saving ? 'Saving...' : 'Save Settings'}
        </button>
      </div>

      {saveMsg && (
        <div className="p-3 bg-emerald-50 dark:bg-emerald-950/50 border border-emerald-500/30 text-emerald-700 dark:text-emerald-300 text-xs font-semibold rounded-lg flex items-center gap-2">
          <CheckCircle2 className="w-4 h-4" /> {saveMsg}
        </div>
      )}

      {/* Security Assurance Notice */}
      <div className="p-4 rounded-xl bg-slate-900 text-slate-200 border border-slate-800 shadow-xs text-xs space-y-2">
        <div className="flex items-center gap-2 text-teal-400 font-bold">
          <ShieldCheck className="w-4 h-4" /> Where your keys are kept
        </div>
        <p className="text-slate-300 text-[11px] leading-relaxed">
          Keys are written as plain text to the <code>.env</code> file on this
          computer, and are <strong>never sent back to this page</strong> -- it only ever
          learns whether a key is set. Nothing scrambles them at rest, so this machine
          is the only thing protecting them.
        </p>
      </div>

      {/* Model Provider Configuration */}
      <div className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs space-y-4">
        <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider flex items-center gap-1.5">
          <Server className="w-4 h-4 text-teal-600" /> 1. LLM Provider &amp; Model Selection
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Provider
            </label>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-900 dark:text-slate-100 font-medium"
            >
              {(modelSettings?.providers || []).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Model Identifier
            </label>
            <input
              id="settings-model"
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="exact model name"
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-900 dark:text-slate-100 font-mono"
            />
            <p className="text-[11px] text-slate-500 mt-1">
              Research, the site brief and the infographic prompt. No example
              is printed here on purpose: a model name that is right today
              stops working without warning, so the only honest list is the
              one your own key answers with.
            </p>
          </div>

          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Message Model <span className="font-normal text-slate-500">(optional)</span>
            </label>
            <input
              id="settings-outreach-model"
              type="text"
              value={outreachModel}
              onChange={(e) => setOutreachModel(e.target.value)}
              placeholder="blank = use the model above"
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-900 dark:text-slate-100 font-mono"
            />
            <p className="text-[11px] text-slate-500 mt-1">
              Writes every message &mdash; the website ones and the infographic
              ones alike. There is no separate model for the picture messages.
              Blank means it uses the model above.
            </p>
          </div>
        </div>

        {/* The other half of "never write a model name into this tool": a way
            to ask. Without it, a retired model left nowhere to look up a
            replacement at the moment every message had stopped being written. */}
        <div className="mt-4 space-y-2">
          <button
            id="settings-catalogue-btn"
            onClick={loadCatalogue}
            disabled={catalogueLoading}
            className="px-3.5 py-2 rounded-lg border border-slate-300 dark:border-slate-700 text-[12px] font-semibold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 studio-transition disabled:opacity-50"
          >
            {catalogueLoading ? 'Asking your provider...' : 'Show the models my key has'}
          </button>

          {catalogueMsg && (
            <p
              id="settings-catalogue-error"
              className="text-[11px] text-amber-700 dark:text-amber-300 whitespace-pre-wrap"
            >
              {catalogueMsg}
            </p>
          )}

          {catalogue && catalogue.length > 0 && (
            <div
              id="settings-catalogue"
              className="max-h-56 overflow-auto rounded-lg border border-slate-200 dark:border-slate-800 divide-y divide-slate-100 dark:divide-slate-800"
            >
              {catalogue.map((name) => (
                <div
                  key={name}
                  className="flex items-center justify-between gap-2 px-2.5 py-1.5"
                >
                  <code className="text-[11px] font-mono text-slate-700 dark:text-slate-300 break-all">
                    {name}
                  </code>
                  <span className="flex gap-1.5 shrink-0">
                    {/* Clickable, because retyping a name read off a screen is
                        how a typo becomes a 404 that looks like a retirement. */}
                    <button
                      id={`settings-pick-model-${name}`}
                      onClick={() => setModel(name)}
                      className="px-2 py-1 rounded-md bg-emerald-500 text-slate-950 text-[11px] font-semibold hover:bg-emerald-400 studio-transition"
                    >
                      Use
                    </button>
                    <button
                      id={`settings-pick-message-${name}`}
                      onClick={() => setOutreachModel(name)}
                      className="px-2 py-1 rounded-md border border-slate-300 dark:border-slate-700 text-[11px] font-semibold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 studio-transition"
                    >
                      For messages
                    </button>
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* API Credentials */}
      <div className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs space-y-4">
        <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider flex items-center gap-1.5">
          <Key className="w-4 h-4 text-teal-600" /> 2. API Keys
        </h2>

        {/* Places Key */}
        <div className="space-y-2 text-xs">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-slate-700 dark:text-slate-300 flex items-center gap-1.5">
              Google Places API Key
              {modelSettings?.places_key_set ? (
                <span className="text-[10px] bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 font-bold px-2 py-0.5 rounded">
                  Set ✓
                </span>
              ) : (
                <span className="text-[10px] bg-amber-500/10 text-amber-600 font-bold px-2 py-0.5 rounded">
                  Not Set
                </span>
              )}
            </span>

            <a
              href="https://developers.google.com/maps/documentation/places/web-service/get-api-key"
              target="_blank"
              rel="noreferrer"
              className="text-teal-600 hover:underline flex items-center gap-0.5 text-[11px]"
            >
              Where do I get this? <ExternalLink className="w-3 h-3" />
            </a>
          </div>

          <div className="flex gap-2">
            <input
              type="password"
              placeholder={modelSettings?.places_key_set ? 'Key stored securely' : 'AIzaSy...'}
              value={placesKeyInput}
              onChange={(e) => setPlacesKeyInput(e.target.value)}
              className="flex-1 p-2 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-xs text-slate-900 dark:text-slate-100"
            />
            <button
              onClick={() => handleTestKey('places')}
              disabled={testPlacesLoading}
              className="px-3 py-2 bg-slate-200 dark:bg-slate-800 hover:bg-slate-300 text-slate-700 dark:text-slate-300 font-semibold rounded-lg text-xs"
            >
              {testPlacesLoading ? 'Testing...' : 'Test Key'}
            </button>
          </div>

          {placesTestMsg && (
            <p className="text-[11px] font-mono text-teal-600 dark:text-teal-400">{placesTestMsg}</p>
          )}
        </div>

        {/* Model Key */}
        <div className="space-y-2 text-xs pt-3 border-t border-slate-100 dark:border-slate-800">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-slate-700 dark:text-slate-300 flex items-center gap-1.5">
              Model API Key{provider ? ` (${provider})` : ''}
              {modelSettings?.key_set ? (
                <span className="text-[10px] bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 font-bold px-2 py-0.5 rounded">
                  Set ✓
                </span>
              ) : (
                <span className="text-[10px] bg-amber-500/10 text-amber-600 font-bold px-2 py-0.5 rounded">
                  Not Set
                </span>
              )}
            </span>

            <a
              href="https://aistudio.google.com/app/apikey"
              target="_blank"
              rel="noreferrer"
              className="text-teal-600 hover:underline flex items-center gap-0.5 text-[11px]"
            >
              Where do I get this? <ExternalLink className="w-3 h-3" />
            </a>
          </div>

          <div className="flex gap-2">
            <input
              type="password"
              placeholder={modelSettings?.key_set ? 'Key stored securely' : 'Enter model key...'}
              value={modelKeyInput}
              onChange={(e) => setModelKeyInput(e.target.value)}
              className="flex-1 p-2 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-xs text-slate-900 dark:text-slate-100"
            />
            <button
              onClick={() => handleTestKey('model')}
              disabled={testModelLoading}
              className="px-3 py-2 bg-slate-200 dark:bg-slate-800 hover:bg-slate-300 text-slate-700 dark:text-slate-300 font-semibold rounded-lg text-xs"
            >
              {testModelLoading ? 'Testing...' : 'Test Key'}
            </button>
          </div>

          {modelTestMsg && (
            <p className="text-[11px] font-mono text-teal-600 dark:text-teal-400">{modelTestMsg}</p>
          )}
        </div>
      </div>

      {/* WhatsApp Local Relay Settings */}
      <div className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs space-y-3">
        <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider flex items-center gap-1.5">
          <Smartphone className="w-4 h-4 text-teal-600" /> 3. WhatsApp Local Relay Configuration
        </h2>

        <div className="text-xs space-y-2">
          <label className="block font-semibold text-slate-700 dark:text-slate-300">
            Local Socket Relay Endpoint
          </label>
          <p className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg font-mono text-xs text-slate-900 dark:text-slate-100">
            {whatsappStatus?.relayUrl || 'Relay not reachable'}
          </p>
          <p className="text-[11px] text-slate-500">
            The relay runs on this computer and holds the WhatsApp session; nothing
            about it leaves the machine. Its port is set in the campaign file, so it
            is shown here rather than edited.
          </p>

          {/* Logging out at WhatsApp's end leaves the saved session on disk,
              so the next start reads the dead one straight back, WhatsApp
              answers 401, and the relay reconnect-loops for ever without
              ever showing a QR. This deletes the folder, which is what makes
              pairing a different phone possible. */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {resetArmed ? (
              <>
                <span className="text-[11px] text-amber-700 dark:text-amber-300">
                  This unlinks the phone and deletes the saved session. You scan
                  a new code to pair again. Sure?
                </span>
                <button
                  id="relay-reset-confirm"
                  onClick={async () => {
                    setResetBusy(true);
                    setResetNote(null);
                    const res = await api.resetWhatsApp();
                    setResetBusy(false);
                    setResetArmed(false);
                    setResetNote(
                      res.error || res.data?.note || 'Unlinked. Scan the new code to pair again.'
                    );
                    loadData();
                  }}
                  disabled={resetBusy}
                  className="px-3 py-1.5 rounded-lg bg-amber-500 text-slate-950 text-[11px] font-semibold hover:bg-amber-400 studio-transition disabled:opacity-50"
                >
                  {resetBusy ? 'Unlinking...' : 'Yes, unlink it'}
                </button>
                <button
                  onClick={() => setResetArmed(false)}
                  className="px-2 py-1.5 text-[11px] text-slate-500 hover:text-slate-300"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                id="relay-reset"
                onClick={() => setResetArmed(true)}
                className="px-3 py-1.5 rounded-lg border border-slate-300 dark:border-slate-700 text-[11px] font-semibold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 studio-transition"
              >
                Log out / pair a different phone
              </button>
            )}
            {resetNote && (
              <span className="text-[11px] text-slate-400 whitespace-pre-wrap">
                {resetNote}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Transparent Data Privacy Notice */}
      <div className="p-4 rounded-xl border bg-slate-50 dark:bg-slate-800/40 border-slate-200 dark:border-slate-700 text-xs text-slate-600 dark:text-slate-300 space-y-2">
        <div className="font-bold text-slate-800 dark:text-slate-200 flex items-center gap-1.5">
          <Info className="w-4 h-4 text-teal-600" /> Privacy &amp; Google Workspace Data Access
        </div>
        <p className="text-[11px] leading-relaxed text-slate-500">
          This application accesses Google Sheets exclusively through the user-granted Google Drive scope (<code>drive.file</code>) to create and sync your pipeline spreadsheet. No personal account data is shared with third parties or stored off-device.
        </p>
      </div>
    </div>
  );
};
