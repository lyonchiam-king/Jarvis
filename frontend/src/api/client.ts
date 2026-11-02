/**
 * The one place that knows what the Python dashboard actually answers.
 *
 * The views are written against the types in `../types`. The Flask API is
 * older than they are and speaks its own shapes -- `name` where a view wants
 * `business_name`, `dialled` where it wants `e164_phone`, a `queues` object
 * where it wants a flat list. Translating here, once, keeps that difference
 * out of every view and means a rename on the Python side is one edit rather
 * than nine.
 *
 * Two rules hold throughout:
 *
 *   - Every request is same-origin and relative. The page is served by the
 *     same Flask app it calls, so the login cookie rides along on its own and
 *     there is no address to configure.
 *   - Nothing is invented. Where Jarvis genuinely does not know something the
 *     field comes back empty and the view is expected to show nothing, not a
 *     plausible-looking default. A dashboard that spends money and messages
 *     real businesses is the worst possible place for a convincing guess.
 */
import {
  Summary,
  CampaignConfig,
  ModelSettings,
  Lead,
  ReachabilityResponse,
  ReachabilityEntry,
  Brief,
  BuildItem,
  SendItem,
  RunLog,
  WhatsAppStatus,
  ApiResponse,
  Stage,
  VisualItem,
  VisualQueue,
  VisualState,
  VisualCandidates,
} from '../types';

/** Everything the Python side may answer with, before translation. */
type Raw = Record<string, any>;

class ApiClient {
  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<ApiResponse<T>> {
    try {
      const res = await fetch(endpoint, {
        // Flask signs the session cookie; without this a cross-origin build
        // would silently drop it and every call would 401.
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
      });

      if (res.status === 401) {
        return { error: 'Signed out. Reload the page to sign in again.' };
      }

      const json = await res.json();
      // Jarvis answers a failed-but-expected call with HTTP 200 and an
      // `error` key -- a Sheet that would not open is not a broken request.
      // Both forms have to be caught or the page renders an empty queue and
      // calls it "nothing to do".
      if (!res.ok || json.error) {
        // The body rides along: an expected failure often carries more than
        // its message -- which of several repairs applies, for one -- and a
        // response collapsed to `{ error }` leaves a view no way to know.
        return {
          error: json.error || `HTTP ${res.status}: ${res.statusText}`,
          raw: json,
        };
      }
      return { data: json, ok: true };
    } catch (err: any) {
      return { error: err?.message || 'Could not reach the dashboard.' };
    }
  }

  private post<T>(endpoint: string, body?: unknown): Promise<ApiResponse<T>> {
    return this.request<T>(endpoint, {
      method: 'POST',
      body: JSON.stringify(body ?? {}),
    });
  }

  /** Re-shape a successful response, leaving errors exactly as they came. */
  private map<A, B>(res: ApiResponse<A>, fn: (data: A) => B): ApiResponse<B> {
    if (res.error || !res.data) return { error: res.error };
    return { data: fn(res.data), ok: true };
  }

  private static num(raw: unknown): number {
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  }

  // ------------------------------------------------------------- overview

  async getSummary(): Promise<ApiResponse<Summary>> {
    const res = await this.request<Raw>('/api/summary');
    return this.map(res, (d) => ({
      // Present only when keys are missing, and then it is the *whole*
      // answer: there are no counts to show because nothing has run.
      setup_needed: d.setup_needed,
      total: ApiClient.num(d.total),
      disqualified: ApiClient.num(d.disqualified),
      by_stage: (d.by_stage || {}) as Record<Stage, number>,
      to_research: ApiClient.num(d.to_research),
      to_brief: ApiClient.num(d.to_brief),
      to_draft: ApiClient.num(d.to_draft),
      awaiting_approval: ApiClient.num(d.awaiting_approval),
      approved_unbuilt: ApiClient.num(d.approved_unbuilt),
      whatsapp_waiting: ApiClient.num(d.whatsapp_waiting),
      errors: ApiClient.num(d.errors),
      errors_retryable: ApiClient.num(d.errors_retryable),
      sheet_url: d.sheet_url || undefined,
    }));
  }

  async createSheet(): Promise<ApiResponse<{ ok: boolean; url: string; title: string }>> {
    return this.post('/api/create-sheet');
  }

  async getLeads(): Promise<ApiResponse<{ leads: Lead[] }>> {
    return this.request<{ leads: Lead[] }>('/api/leads');
  }

  async updateLeadNote(lead_id: string, note: string): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/lead-note', { lead_id, note });
  }

  async getNextUp(): Promise<ApiResponse<{ names: string[]; capped: boolean }>> {
    const res = await this.request<Raw>('/api/next-up');
    return this.map(res, (d) => {
      // Two queues come back -- who would be researched, and who would be
      // briefed. The header wants one list of names, in that order.
      const queues = d.queues || {};
      const names: string[] = [];
      for (const stage of ['research', 'site-brief']) {
        for (const row of queues[stage] || []) names.push(row.name || row.lead_id);
      }
      return { names, capped: names.length >= ApiClient.num(d.cap) };
    });
  }

  // -------------------------------------------------------------- campaign

  async getCampaign(): Promise<ApiResponse<CampaignConfig>> {
    const res = await this.request<Raw>('/api/campaign');
    return this.map(res, (d) => ({
      category: d.category || '',
      location: d.location || '',
      areas: d.areas || [],
      sender_name: d.sender_name || '',
      daily_batch_limit: ApiClient.num(d.batch_size),
      country_code: d.country_code || '',
      config_file: d.file || '',
    }));
  }

  async saveCampaign(config: Partial<CampaignConfig>): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/campaign', {
      category: config.category,
      location: config.location,
      areas: config.areas,
      sender_name: config.sender_name,
      batch_size: config.daily_batch_limit,
    });
  }

  async suggestAreas(location: string): Promise<ApiResponse<{ areas: string[] }>> {
    return this.post('/api/suggest-areas', { location });
  }

  // -------------------------------------------------------------- settings

  async getModel(): Promise<ApiResponse<ModelSettings>> {
    const res = await this.request<Raw>('/api/model');
    return this.map(res, (d) => ({
      provider: d.provider || '',
      model: d.model || '',
      outreach_model: d.outreach_model || '',
      base_url: d.base_url || '',
      providers: d.providers || [],
      key_env: d.key_env || '',
      key_needed: !!d.key_needed,
      key_set: !!d.key_set,
      places_key_set: !!d.places_key_set,
    }));
  }

  async saveModel(settings: {
    provider?: string;
    model?: string;
    outreach_model?: string;
    base_url?: string;
    api_key?: string;
    places_key?: string;
  }): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/model', settings);
  }

  async testKey(
    type: 'places' | 'model',
    key?: string,
    extra: { provider?: string; model?: string } = {}
  ): Promise<ApiResponse<{ valid: boolean; message: string }>> {
    return this.post('/api/test-key', { type, key, ...extra });
  }

  async getDesignLevel(): Promise<ApiResponse<{ level: string; levels: string[] }>> {
    return this.request('/api/design-level');
  }

  async saveDesignLevel(level: string): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/design-level', { level });
  }

  async getServices(): Promise<ApiResponse<{ intro: string; services: string[] }>> {
    return this.request('/api/services');
  }

  async saveServices(intro: string, services: string[]): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/services', { intro, services });
  }

  // ---------------------------------------------------------- reachability

  async getReachability(): Promise<ApiResponse<ReachabilityResponse>> {
    const res = await this.request<Raw>('/api/reachability');
    const entry = (verdict: 'yes' | 'no' | 'unknown') => (e: Raw): ReachabilityEntry => ({
      whatsapp_reachable: verdict,
      lead_id: e.lead_id,
      business_name: e.name || e.lead_id,
      phone: e.phone || '',
      e164_phone: e.dialled || '',
      email_found: e.email ? 'yes' : '',
      checked_at: e.checked_at || '',
      // Said in the pipeline's own terms rather than invented: a lead is
      // built for when WhatsApp confirmed it, or when there is an email to
      // fall back on. Anything else is waiting on the relay, not rejected.
      buildable_reason: e.email
        ? 'Has an email address'
        : 'Needs a confirmed WhatsApp number',
    });
    return this.map(res, (d) => ({
      // The list a lead arrives in *is* the verdict -- Python has already
      // read the cached answer and sorted on it.
      verified: (d.verified || []).map(entry('yes')),
      rejected: (d.rejected || []).map(entry('no')),
      waiting: (d.waiting || []).map(entry('unknown')),
      counts: {
        verified: ApiClient.num(d.counts?.verified),
        rejected: ApiClient.num(d.counts?.rejected),
        waiting: ApiClient.num(d.counts?.waiting),
        buildable: ApiClient.num(d.counts?.buildable),
      },
    }));
  }

  /**
   * Ask WhatsApp about the leads nobody has asked about yet.
   *
   * There is no separate endpoint for this: reachability is a pipeline stage,
   * so it runs the same way every other stage does and its progress appears
   * in the run log. The button is fire-and-forget for that reason -- the
   * answer arrives in the log and in the next refresh, not in this response.
   */
  async checkWhatsAppBatch(): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/run', { what: 'reachability' });
  }

  // ---------------------------------------------------------------- briefs

  async getReviewQueue(): Promise<ApiResponse<{ queue: Brief[] }>> {
    const res = await this.request<Raw>('/api/review-queue');
    return this.map(res, (d) => ({
      queue: (d.queue || []).map(
        (b: Raw): Brief => ({
          lead_id: b.lead_id,
          business_name: b.name || b.lead_id,
          lovable_prompt: b.prompt || '',
          // The queue only ever holds briefs nobody has decided on, so this
          // is a fact about the endpoint rather than a placeholder.
          approval_status: 'Pending',
          research_summary: {
            category: b.category || '',
            angle: b.angle || '',
            praise: b.praise || '',
          },
          created_at: '',
        })
      ),
    }));
  }

  async decideBrief(
    lead_id: string,
    decision: 'approve' | 'skip' | 'later'
  ): Promise<ApiResponse<{ ok: boolean }>> {
    if (decision === 'later') {
      // Leaving it alone *is* "later": an undecided brief stays at the front
      // of the queue. Writing a third status would take it out of the queue
      // and there would be no screen that showed it again.
      return { data: { ok: true }, ok: true };
    }
    return this.post('/api/decide', {
      lead_id,
      decision: decision === 'approve' ? 'approved' : 'skip',
    });
  }

  async regenerateBrief(lead_id: string, notes = ''): Promise<ApiResponse<{ prompt: string }>> {
    return this.post('/api/regenerate', { lead_id, notes });
  }

  async dropLead(lead_id: string): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/drop', { lead_id });
  }

  // ----------------------------------------------------------------- build

  async getBuildQueue(): Promise<ApiResponse<{ queue: BuildItem[] }>> {
    const res = await this.request<Raw>('/api/build-queue');
    return this.map(res, (d) => ({
      queue: (d.queue || []).map(
        (b: Raw): BuildItem => ({
          lead_id: b.lead_id,
          business_name: b.name || b.lead_id,
          lovable_prompt: b.prompt || '',
          // The build queue is deliberately thin on the Python side -- it
          // carries the prompt and nothing else. Left empty rather than
          // filled with something that reads like the real address.
          category: '',
          address: '',
          preview_url: '',
          stage: 'Site Briefed' as Stage,
          country: '',
          created_at: '',
        })
      ),
    }));
  }

  async recordBuild(lead_id: string, preview_url: string): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/record-build', { lead_id, preview_url });
  }

  // ---------------------------------------------------------- the graphic

  /**
   * The infographic queue. `enabled` false means this campaign pitches a
   * page, so the stage is not shown at all -- a panel that can never do
   * anything reads as broken.
   */
  async getVisualQueue(): Promise<ApiResponse<VisualQueue> & { missingTab?: boolean }> {
    const res = await this.request<Raw>('/api/visual/queue');
    // `request` collapses a body carrying `error` into a failed response, so
    // the flag sitting beside it would be lost. Read off the raw body first.
    const missingTab = !!(res.raw && res.raw.missing_tab);
    if (res.error) return { error: res.error, missingTab };
    return this.map(res, (d) => ({
      enabled: !!d.enabled,
      missing_tab: !!d.missing_tab,
      track: d.track || 'website',
      tool_name: d.tool_name || '',
      // Left empty rather than defaulted to a guess: a button pointing at the
      // wrong address is worse than no button, and nothing here knows the
      // real one.
      tool_url: d.tool_url || '',
      signature: d.signature || '',
      counts: (d.counts || {}) as Record<VisualState, number>,
      catalogue: {
        styles: d.catalogue?.styles || [],
        layouts: d.catalogue?.layouts || [],
        chosen_styles: d.catalogue?.chosen_styles || [],
        chosen_layouts: d.catalogue?.chosen_layouts || [],
      },
      queue: (d.queue || []).map(
        (v: Raw): VisualItem => ({
          lead_id: v.lead_id,
          business_name: v.name || v.lead_id,
          category: v.category || '',
          prompt: v.prompt || '',
          gaps: v.gaps || '',
          style: v.style || '',
          layout: v.layout || '',
          specific: !!v.specific,
          state: (v.state || 'needs_image') as VisualState,
          approval: v.approval || '',
          expected_text: v.expected_text || [],
          message: v.message || '',
          sent_at: v.sent_at || '',
          created_at: v.created_at || '',
        })
      ),
    }));
  }

  /**
   * Who a run would write for, split by how they can be contacted.
   *
   * Sixty prompts is not a batch anybody works through -- each one is a
   * visit to the image tool and a picture to read -- and a limit alone picks
   * whichever leads come first rather than the ones worth starting with.
   */
  async getVisualCandidates(): Promise<ApiResponse<VisualCandidates>> {
    const res = await this.request<Raw>('/api/visual/candidates');
    return this.map(res, (d) => ({
      enabled: !!d.enabled,
      total: ApiClient.num(d.total),
      groups: (d.groups || []).map((g: Raw) => ({
        key: g.key,
        label: g.label || g.key,
        count: ApiClient.num(g.count),
        contactable: !!g.contactable,
        capped: !!g.capped,
        leads: g.leads || [],
      })),
    }));
  }

  /** Write prompts for one group, or for an explicit pick. */
  async runVisual(
    groups: string[],
    lead_ids: string[],
    limit: number | null
  ): Promise<ApiResponse<{ ok: boolean; running: string }>> {
    return this.post('/api/visual/run', { groups, lead_ids, limit });
  }

  /** Hand the picture back: the bytes as a data URL, or an address. */
  async attachVisual(
    lead_id: string,
    source: { data_url?: string; url?: string }
  ): Promise<ApiResponse<{ ok: boolean; state: VisualState; bytes: number }>> {
    return this.post('/api/visual/attach', { lead_id, ...source });
  }

  /**
   * Record that a person has read the words on the picture. The gate, not
   * advice: nothing here can test that the text rendered correctly, and a
   * garbled word on a graphic carrying your name cannot be fixed after it
   * has been sent.
   */
  async confirmVisual(
    lead_id: string
  ): Promise<ApiResponse<{ ok: boolean; state: VisualState }>> {
    return this.post('/api/visual/confirm', { lead_id });
  }

  async clearVisual(
    lead_id: string
  ): Promise<ApiResponse<{ ok: boolean; state: VisualState }>> {
    return this.post('/api/visual/clear', { lead_id });
  }

  async decideVisual(
    lead_id: string,
    decision: 'Skip' | 'Approved'
  ): Promise<ApiResponse<{ ok: boolean; approval: string }>> {
    return this.post('/api/visual/skip', { lead_id, decision });
  }

  async regenerateVisual(
    lead_id: string
  ): Promise<ApiResponse<{ ok: boolean; prompt: string; style: string; layout: string }>> {
    return this.post('/api/visual/regenerate', { lead_id });
  }

  async getTrack(): Promise<
    ApiResponse<{
      track: string;
      tracks: string[];
      signature: string;
      tool_name: string;
      tool_url: string;
      /**
       * Offered to the field, never stored on the user's behalf. Nothing in
       * Jarvis can reach that host to check it, so the value sits in front of
       * them before they save -- the difference between confirming an address
       * and us guessing one.
       */
      tool_url_suggestion: string;
    }>
  > {
    return this.request('/api/track');
  }

  async setTrack(
    track: string,
    signature: string,
    tool?: { name: string; url: string }
  ): Promise<ApiResponse<{ ok: boolean; track: string; tool_url: string }>> {
    return this.post('/api/track', {
      track,
      signature,
      // Omitted rather than sent empty when there is nothing to say: the
      // server reads absent as "leave it alone" and empty as "no button".
      ...(tool ? { tool_name: tool.name, tool_url: tool.url } : {}),
    });
  }

  /**
   * Add any tab this version needs that the Sheet does not have.
   *
   * Idempotent, and it touches no data row -- only missing tabs and row 1.
   * It existed as `outreach init-sheet` long before it had a button, which
   * is the same as not existing for someone who does not use a terminal.
   */
  async repairTabs(): Promise<ApiResponse<{ ok: boolean; added: string[]; note: string }>> {
    return this.post('/api/repair-tabs');
  }

  async setVisualRotation(
    styles: string[],
    layouts: string[]
  ): Promise<ApiResponse<{ ok: boolean; styles: string[]; layouts: string[] }>> {
    return this.post('/api/visual/rotation', { styles, layouts });
  }

  // ------------------------------------------------------------------ send

  async getSendQueue(): Promise<
    ApiResponse<{ queue: SendItem[]; excluded: { name: string; phone: string; reason: string }[] }>
  > {
    const res = await this.request<Raw>('/api/whatsapp/queue');
    return this.map(res, (d) => ({
      queue: (d.queue || []).map(
        (s: Raw): SendItem => ({
          lead_id: s.lead_id,
          business_name: s.name || s.lead_id,
          recipient_phone: s.phone || '',
          e164_phone: s.phone_e164 || '',
          whatsapp_message: s.message || '',
          // Which picture rides with it. The queue has always said; nothing
          // read it, so an infographic was labelled a site screenshot.
          shot_kind: s.shot_kind === 'visual' ? 'visual' : 'site',
          // Only offered when a picture actually exists on disk: the queue
          // says so per lead, and a broken image is worse than none.
          screenshot_url: s.has_shot ? `/api/screenshot/${encodeURIComponent(s.lead_id)}` : '',
          // The relay-free way to send. Python built it where the lead's
          // country was known; empty when there is no dialable number.
          wa_link: s.wa_link || '',
          category: '',
          preview_url: '',
          stage: 'Site Built' as Stage,
          drafted_at: '',
        })
      ),
      excluded: d.excluded || [],
    }));
  }

  async sendWhatsApp(
    lead_id: string,
    customMessage?: string,
    phone_e164?: string
  ): Promise<ApiResponse<{ status: string; note?: string }>> {
    // `phone_e164` is computed by Python, where the lead's country is known,
    // and handed straight back. The browser must never work a dialling code
    // out for itself: a number normalised twice grows a second country code
    // and the message goes to a stranger.
    return this.post('/api/whatsapp/send', {
      lead_id,
      message: customMessage,
      phone_e164,
    });
  }

  async rewriteOutreach(lead_id: string, instruction: string): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/outreach/rewrite', { lead_id, notes: instruction });
  }

  async undoWhatsAppSend(lead_id: string): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/whatsapp/undo-send', { lead_id });
  }

  async skipWhatsApp(lead_id: string): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/whatsapp/skip', { lead_id });
  }

  /**
   * Record that you sent one yourself, from your own phone.
   *
   * Following a click-to-chat link tells this page nothing at all, so
   * without this the lead sits at the top of the queue for ever and the
   * same business gets messaged again tomorrow. It is stored as
   * `unconfirmed` because it is a claim, not something observed.
   */
  async markWhatsAppSent(lead_id: string): Promise<ApiResponse<{ status: string }>> {
    return this.post('/api/whatsapp/mark-sent', { lead_id });
  }

  // ----------------------------------------------------------- relay + run

  async getWhatsAppStatus(): Promise<ApiResponse<WhatsAppStatus>> {
    const res = await this.request<Raw>('/api/whatsapp/status');
    return this.map(res, (d) => ({
      enabled: !!d.enabled,
      connected: !!d.connected,
      // "Connecting" and "not connected" are different sentences: one needs
      // nothing from the user and the other needs the relay restarted.
      // Collapsing them made every ordinary reconnect look like a dead relay.
      state: !d.enabled
        ? 'idle'
        : d.connected
          ? 'connected'
          : d.resetting
            ? 'linking'
            : d.connecting || d.reconnecting
              ? 'linking'
              : d.error || d.last_error
                ? 'error'
                : 'disconnected',
      error: d.error || d.last_error || '',
      relayUrl: d.relay_url || '',
    }));
  }

  async getWhatsAppQr(): Promise<ApiResponse<{ qr: string | null }>> {
    return this.request('/api/whatsapp/qr');
  }

  async resetWhatsApp(): Promise<ApiResponse<{ ok: boolean; note?: string }>> {
    return this.post('/api/whatsapp/reset');
  }

  async runBatch(what: string, limit?: number): Promise<ApiResponse<{ ok: boolean }>> {
    return this.post('/api/run', { what, limit });
  }

  async getLog(): Promise<ApiResponse<RunLog>> {
    return this.request<RunLog>('/api/log');
  }
}

export const api = new ApiClient();
