import React, { useState, useEffect } from 'react';
import {
  Compass,
  Sparkles,
  MapPin,
  Plus,
  X,
  Sliders,
  Globe,
  Save,
  CheckCircle2,
} from 'lucide-react';
import { CampaignConfig } from '../types';
import { api } from '../api/client';

/**
 * Which artefact the message carries.
 *
 * Saved on its own, not with the rest of the campaign: it is one decision
 * with its own refusal (a graphics track needs a signature, or the tool will
 * not start), and folding it into Save Configuration would make that refusal
 * reject everything else on this screen with it.
 */
const TrackPicker: React.FC = () => {
  const [track, setTrack] = useState('website');
  const [signature, setSignature] = useState('');
  const [toolName, setToolName] = useState('');
  const [toolUrl, setToolUrl] = useState('');
  const [note, setNote] = useState('');
  const [bad, setBad] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getTrack().then((res) => {
      if (res.data) {
        setTrack(res.data.track || 'website');
        setSignature(res.data.signature || '');
        setToolName(res.data.tool_name || '');
        // Prefilled with the suggestion only while nothing is saved, so what
        // is in the box is what Save will store -- and a cleared address
        // stays cleared instead of reappearing on the next load.
        setToolUrl(res.data.tool_url || res.data.tool_url_suggestion || '');
      }
    });
  }, []);

  const needsSignature = track === 'visual' || track === 'both';

  const save = async () => {
    setSaving(true);
    setNote('');
    const res = await api.setTrack(track, signature, {
      name: toolName,
      url: toolUrl,
    });
    setSaving(false);
    setBad(!!res.error);
    setNote(res.error || 'Saved.');
  };

  const choice = (value: string, label: string, blurb: string) => (
    <label
      key={value}
      className={`block p-3 rounded-lg border cursor-pointer studio-transition ${
        track === value
          ? 'border-emerald-500/50 bg-emerald-500/10'
          : 'border-slate-700 hover:border-slate-600'
      }`}
    >
      <span className="flex items-start gap-2">
        <input
          type="radio"
          name="outreach-track"
          value={value}
          checked={track === value}
          onChange={() => setTrack(value)}
          className="mt-0.5"
        />
        <span>
          <span className="block text-[13px] font-medium text-slate-200">{label}</span>
          <span className="block text-[11px] text-slate-500">{blurb}</span>
        </span>
      </span>
    </label>
  );

  return (
    <div
      id="track-picker"
      className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 space-y-3"
    >
      <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider">
        What the message pitches
      </h2>
      <p className="text-[11px] text-slate-500">
        One setting, not three that can disagree. It decides what gets
        written, what the send step waits for, and what the message claims.
      </p>
      <div className="space-y-2">
        {choice('website', 'Website', 'Build a demo page and link to it. The original.')}
        {choice('visual', 'Infographic', 'A picture naming the gaps. No site is built.')}
        {choice(
          'both',
          'Both',
          'The picture attached and the link beside it — still one message per business.',
        )}
      </div>
      {needsSignature && (
        <div>
          <label className="block text-[12px] font-medium text-slate-300 mb-1">
            The ribbon on every graphic, word for word
          </label>
          <input
            id="track-signature"
            type="text"
            value={signature}
            onChange={(e) => setSignature(e.target.value)}
            placeholder="by Lyon, Business Growth Consultant"
            className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-[13px] text-slate-900 dark:text-slate-100"
          />
          <p className="text-[11px] text-slate-500 mt-1">
            Required for a graphic. It is the one thing that makes the series
            read as yours, so the tool will not start without it.
          </p>
        </div>
      )}
      {needsSignature && (
        <div className="space-y-2">
          <div>
            <label className="block text-[12px] font-medium text-slate-300 mb-1">
              Where you make the pictures
            </label>
            <input
              id="tool-name"
              type="text"
              value={toolName}
              onChange={(e) => setToolName(e.target.value)}
              placeholder="Google Flow"
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-[13px] text-slate-900 dark:text-slate-100"
            />
          </div>
          <div>
            <label className="block text-[12px] font-medium text-slate-300 mb-1">
              Its web address
            </label>
            <input
              id="tool-url"
              type="url"
              value={toolUrl}
              onChange={(e) => setToolUrl(e.target.value)}
              placeholder="https://..."
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-[13px] text-slate-900 dark:text-slate-100"
            />
            <p className="text-[11px] text-slate-500 mt-1">
              This becomes the <strong>Open</strong> button on every card in
              the Infographic step. The address above is a suggestion, not
              something checked — nothing here can reach that site to confirm
              it. If it is not where the tool opens for you, copy the address
              out of your browser and paste it here. Clear the box and the
              button simply does not appear.
            </p>
          </div>
        </div>
      )}

      <div className="flex items-center gap-3">
        <button
          id="save-track-btn"
          onClick={save}
          disabled={saving}
          className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-emerald-500 text-slate-950 text-[12px] font-semibold hover:bg-emerald-400 studio-transition disabled:opacity-50"
        >
          <Save className="w-3.5 h-3.5" />
          {saving ? 'Saving...' : 'Save'}
        </button>
        {note && (
          <span
            className={`text-[12px] whitespace-pre-wrap ${bad ? 'text-amber-300' : 'text-emerald-300'}`}
          >
            {note}
          </span>
        )}
      </div>
    </div>
  );
};

export const CampaignView: React.FC = () => {
  const [config, setConfig] = useState<CampaignConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [newAreaInput, setNewAreaInput] = useState('');
  const [saveSuccess, setSaveSuccess] = useState(false);

  const loadConfig = async () => {
    setLoading(true);
    const res = await api.getCampaign();
    if (res.data) setConfig(res.data);
    setLoading(false);
  };

  useEffect(() => {
    loadConfig();
  }, []);

  const handleSuggestAreas = async () => {
    if (!config?.location) return;
    setSuggesting(true);
    const res = await api.suggestAreas(config.location);
    setSuggesting(false);
    if (res.data?.areas && config) {
      const merged = Array.from(new Set([...config.areas, ...res.data.areas]));
      setConfig({ ...config, areas: merged });
    }
  };

  const handleAddArea = () => {
    if (!newAreaInput.trim() || !config) return;
    if (!config.areas.includes(newAreaInput.trim())) {
      setConfig({ ...config, areas: [...config.areas, newAreaInput.trim()] });
    }
    setNewAreaInput('');
  };

  const handleRemoveArea = (area: string) => {
    if (!config) return;
    setConfig({
      ...config,
      areas: config.areas.filter((a) => a !== area),
    });
  };

  const handleSave = async () => {
    if (!config) return;
    setSaving(true);
    await api.saveCampaign(config);
    setSaving(false);
    setSaveSuccess(true);
    setTimeout(() => setSaveSuccess(false), 3000);
  };

  if (loading || !config) {
    return (
      <div id="campaign-loading" className="p-8 text-center text-slate-500 animate-pulse">
        Loading campaign parameters...
      </div>
    );
  }

  return (
    <div id="campaign-view" className="space-y-6 max-w-3xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs">
        <div>
          <h1 className="text-lg font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
            <Compass className="w-5 h-5 text-teal-600" /> Lead Targeting &amp; Campaign Parameters
          </h1>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
            Configure niche category, geographic search areas, and automated batch limits.
          </p>
        </div>

        <button
          id="save-campaign-btn"
          onClick={handleSave}
          disabled={saving}
          className="inline-flex items-center gap-1.5 px-4 py-2 bg-teal-600 hover:bg-teal-500 text-white font-bold text-xs rounded-md shadow-xs transition"
        >
          <Save className="w-3.5 h-3.5" />
          {saving ? 'Saving...' : 'Save Configuration'}
        </button>
      </div>

      {saveSuccess && (
        <div className="p-3 bg-emerald-50 dark:bg-emerald-950/50 border border-emerald-500/30 text-emerald-700 dark:text-emerald-300 text-xs font-semibold rounded-lg flex items-center gap-2">
          <CheckCircle2 className="w-4 h-4" /> Campaign parameters saved successfully!
        </div>
      )}

      <TrackPicker />

      {/* Primary Niche & Location */}
      <div className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs space-y-4">
        <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider">
          1. Niche &amp; Target City
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Target Industry / Business Category
            </label>
            <input
              type="text"
              value={config.category}
              onChange={(e) => setConfig({ ...config, category: e.target.value })}
              placeholder="e.g. Physiotherapy Clinics, HVAC Repair"
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-900 dark:text-slate-100 font-medium"
            />
          </div>

          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Target City / Metro Region
            </label>
            <input
              type="text"
              value={config.location}
              onChange={(e) => setConfig({ ...config, location: e.target.value })}
              placeholder="e.g. Austin, TX"
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-900 dark:text-slate-100 font-medium"
            />
          </div>
        </div>
      </div>

      {/* Target Neighborhood Areas */}
      <div className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider flex items-center gap-1.5">
              <MapPin className="w-4 h-4 text-teal-600" /> 2. Target Neighborhood Areas ({config.areas.length})
            </h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
              Specific neighborhoods to query via Google Places API for maximum coverage.
            </p>
          </div>

          <button
            id="suggest-areas-btn"
            onClick={handleSuggestAreas}
            disabled={suggesting || !config.location}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-teal-50 dark:bg-teal-950/60 text-teal-700 dark:text-teal-300 border border-teal-200 dark:border-teal-800 rounded-md text-xs font-semibold hover:bg-teal-100 dark:hover:bg-teal-900 transition"
          >
            <Sparkles className="w-3.5 h-3.5 text-teal-600" />
            {suggesting ? 'AI Suggesting...' : 'Suggest Areas with AI'}
          </button>
        </div>

        {/* Chips Container */}
        <div className="flex flex-wrap gap-2 p-3 bg-slate-50 dark:bg-slate-800/50 rounded-lg border border-slate-200 dark:border-slate-700 min-h-24">
          {config.areas.map((area) => (
            <span
              key={area}
              className="inline-flex items-center gap-1.5 px-3 py-1 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-full text-xs font-medium text-slate-800 dark:text-slate-200 shadow-2xs"
            >
              {area}
              <button
                onClick={() => handleRemoveArea(area)}
                className="text-slate-400 hover:text-red-500 rounded-full p-0.5"
              >
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>

        {/* Add Area Input */}
        <div className="flex gap-2">
          <input
            type="text"
            value={newAreaInput}
            onChange={(e) => setNewAreaInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAddArea()}
            placeholder="Add a custom neighborhood area (e.g. Hyde Park)..."
            className="flex-1 p-2 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-xs text-slate-900 dark:text-slate-100"
          />
          <button
            onClick={handleAddArea}
            className="px-4 py-2 bg-slate-800 dark:bg-slate-700 hover:bg-slate-700 text-white rounded-lg text-xs font-semibold"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Batch & Limits Config */}
      <div className="p-5 rounded-xl border bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800 shadow-xs space-y-4">
        <h2 className="text-xs font-bold text-slate-800 dark:text-slate-200 uppercase tracking-wider flex items-center gap-1.5">
          <Sliders className="w-4 h-4 text-teal-600" /> 3. Batch Sizes &amp; Country Compliance
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Daily Max Batch Limit
            </label>
            <input
              type="number"
              value={config.daily_batch_limit}
              onChange={(e) =>
                setConfig({ ...config, daily_batch_limit: parseInt(e.target.value) || 20 })
              }
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-900 dark:text-slate-100 font-bold"
            />
          </div>

          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Sender name on messages
            </label>
            <input
              type="text"
              value={config.sender_name}
              onChange={(e) => setConfig({ ...config, sender_name: e.target.value })}
              className="w-full p-2.5 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-900 dark:text-slate-100 font-bold"
            />
          </div>

          <div>
            <label className="block font-semibold text-slate-700 dark:text-slate-300 mb-1">
              Compliance country
            </label>
            <p className="w-full p-2.5 bg-slate-100 dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-lg text-slate-500 dark:text-slate-400 font-mono uppercase">
              {config.country_code || 'not set'}
            </p>
            <p className="mt-1 text-[11px] text-slate-500">
              Set in {config.config_file || 'the campaign file'}. Each lead's own
              country is read from its phone number, not from this.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};
