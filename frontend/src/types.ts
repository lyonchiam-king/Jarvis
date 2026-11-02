/**
 * Outreach Studio - Data Models and API Types
 */

export type Stage =
  | 'Discovered'
  | 'Researched'
  | 'Site Briefed'
  | 'Site Built'
  | 'Email Drafted'
  | 'Sent'
  | 'Replied'
  | 'Closed'
  | 'Disqualified';

export type WebsiteStatus = 'none' | 'social_only' | 'real';

export type WhatsAppReachable = 'yes' | 'no' | 'unknown';

export type BriefApprovalStatus = 'Pending' | 'Approved' | 'Skip' | 'Later';

export type SendStatus = 'sent' | 'delivered' | 'read' | 'unconfirmed' | 'failed';

export interface Lead {
  place_id: string; // Lead ID (Google place_id)
  stage: Stage;
  business_name: string;
  category: string;
  address: string;
  phone: string;
  rating: number;
  review_count: number;
  website_status: WebsiteStatus;
  website_url?: string;
  maps_url?: string;
  campaign: string;
  country: string;
  discovered_at: string;
  last_refreshed: string;
  // Manual columns (human sets these directly)
  priority?: 'low' | 'medium' | 'high';
  owner_notes?: string;
  next_action?: string;
  whatsapp_reachable: WhatsAppReachable;
  whatsapp_checked_at?: string;
}

export interface Research {
  lead_id: string;
  business_name: string;
  tone: string;
  services: string[];
  price_tier: string;
  customer_praise: string[];
  customer_pain_points: string[];
  suggested_website_angle: string;
  social_handle?: string;
  social_bio?: string;
  social_recent_post?: string;
  social_lookup_status: 'found' | 'none' | 'pending';
  email_found?: string;
  email_source?: string;
  research_status: 'complete' | 'pending' | 'failed';
  researched_at: string;
  social_profiles: { platform: string; url: string }[];
  whatsapp_reachable: WhatsAppReachable;
  whatsapp_checked_at?: string;
}

export interface Brief {
  lead_id: string;
  business_name: string;
  lovable_prompt: string;
  approval_status: BriefApprovalStatus; // Manual column
  lovable_project_id?: string;
  preview_url?: string;
  created_at: string;
  research_summary?: {
    category: string;
    angle: string;
    praise: string;
  };
}

export interface BuildItem {
  lead_id: string;
  business_name: string;
  category: string;
  lovable_prompt: string;
  preview_url?: string;
  stage: Stage;
  country: string;
  address: string;
  created_at: string;
}

export interface SendItem {
  lead_id: string;
  business_name: string;
  /**
   * Which picture rides with the message: the infographic that IS the pitch
   * on the visual track, or a screenshot of the built page. Naming it wrong
   * describes the thing being sent as something else.
   */
  shot_kind?: 'visual' | 'site';
  /**
   * This message was written for the website pitch and still links to the
   * demo site, on a campaign that now sends a picture on its own. Changing
   * the track changes what is drafted next; it cannot reach back into
   * messages already on the Sheet, and those are what this queue sends.
   */
  stale_pitch?: boolean;
  category: string;
  recipient_phone: string;
  e164_phone: string;
  whatsapp_message: string;
  preview_url: string;
  screenshot_url?: string;
  stage: Stage;
  drafted_at: string;
  status?: SendStatus;
  status_detail?: string;
  /** Click-to-chat link, built server-side where the lead's country was
   *  known. Only its `?text=` is ever replaced here; the number in it is
   *  never rebuilt by the browser. */
  wa_link?: string;
}

export interface OutreachLogRow {
  lead_id: string;
  business_name: string;
  channel: 'whatsapp' | 'email';
  recipient: string;
  email_source?: string;
  subject?: string;
  body_preview: string;
  gmail_draft_id?: string;
  gmail_draft_link?: string;
  whatsapp_message: string;
  drafted_at: string;
  // Manual columns
  status: SendStatus;
  sent_at?: string;
  notes?: string;
  whatsapp_sent_at?: string;
  whatsapp_status?: SendStatus;
}

export interface ErrorItem {
  id: string;
  timestamp: string;
  stage: Stage;
  lead_id: string;
  business_name: string;
  error_type: string;
  message: string;
  retryable: boolean;
}

export interface Summary {
  setup_needed?: string[];
  total: number;
  disqualified: number;
  by_stage: Record<Stage, number>;
  to_research: number;
  to_brief: number;
  to_draft: number;
  awaiting_approval: number;
  approved_unbuilt: number;
  whatsapp_waiting: number;
  errors: number;
  errors_retryable: number;
  sheet_url?: string;
}

export interface CampaignConfig {
  category: string;
  location: string;
  areas: string[];
  sender_name: string;
  daily_batch_limit: number;
  /** Where the compliance footer says the message comes from. Read-only. */
  country_code: string;
  /** Which campaign file the dashboard is actually reading. Read-only. */
  config_file: string;
}

export type CampaignSettings = CampaignConfig;

export interface ModelSettings {
  provider: string;
  model: string;
  outreach_model: string;
  base_url: string;
  /** Every provider the Python side will accept, so the page never offers one it will not. */
  providers: string[];
  key_env: string;
  key_needed: boolean;
  key_set: boolean;
  places_key_set: boolean;
}

export interface WhatsAppStatus {
  enabled: boolean;
  connected: boolean;
  state: 'idle' | 'linking' | 'connected' | 'disconnected' | 'error';
  error: string;
  relayUrl: string;
}

export interface ReachabilityEntry {
  lead_id: string;
  business_name: string;
  phone: string;
  e164_phone: string;
  whatsapp_reachable: WhatsAppReachable;
  email_found?: string;
  checked_at?: string;
  buildable_reason: string;
}

export interface ReachabilityResponse {
  verified: ReachabilityEntry[];
  rejected: ReachabilityEntry[];
  waiting: ReachabilityEntry[];
  counts: {
    verified: number;
    rejected: number;
    waiting: number;
    buildable: number; // Includes no-whatsapp leads if email exists!
  };
}

export interface RunLog {
  running: string | null;
  lines: string[];
}

export interface ApiResponse<T> {
  data?: T;
  error?: string;
  ok?: boolean;
  /**
   * The body exactly as Flask sent it.
   *
   * A failed-but-expected call answers HTTP 200 with an `error` key *and*
   * whatever else it wanted to say -- a flag saying the error has a repair
   * button, for instance. Collapsing that body into `{ error }` threw the
   * rest away, so a view could never act on it.
   */
  raw?: Record<string, any>;
}

// --------------------------------------------------------- the visual track

/**
 * Where one graphic is up to. Four states, because four things are genuinely
 * different and each needs a different button: retired by hand, waiting for
 * the picture, waiting for a person to read the words on it, and ready.
 */
export type VisualState = 'skipped' | 'needs_image' | 'needs_check' | 'ready';

export interface VisualItem {
  lead_id: string;
  business_name: string;
  category: string;
  /** The prompt to paste into the image tool. */
  prompt: string;
  /**
   * One line per card: the rule that chose it and the evidence behind it.
   * Shown so a claim can be checked against the row that justified it
   * before it reaches a real business.
   */
  gaps: string;
  style: string;
  layout: string;
  /** False means the generic graphic -- no name on it, no claim about them. */
  specific: boolean;
  state: VisualState;
  approval: string;
  /**
   * Every string that must appear on the picture, in reading order. Nothing
   * in Jarvis can check that they rendered -- the image is made outside it --
   * so these exist to be read against the picture by a person.
   */
  expected_text: string[];
  /**
   * The real WhatsApp text, written per business by the draft stage. Not
   * `expected_text`, which belongs on the picture and is never sent -- the
   * two were read as one thing, which is the mistake this field exists to
   * stop.
   */
  message: string;
  sent_at: string;
  created_at: string;
}

export interface VisualQueue {
  /** False on the original track: the campaign pitches a page, not a picture. */
  enabled: boolean;
  /**
   * Set when the read failed because this Sheet predates a tab the schema
   * has since grown. Flagged by the server rather than matched on the
   * error's wording: a view that chose a button by searching a message
   * would break the next time the sentence was improved.
   */
  missing_tab?: boolean;
  track: string;
  tool_name: string;
  /** Empty when no address is configured. The view then shows no button. */
  tool_url: string;
  signature: string;
  queue: VisualItem[];
  counts: Record<VisualState, number>;
  catalogue: {
    styles: string[];
    layouts: string[];
    chosen_styles: string[];
    chosen_layouts: string[];
  };
}

/** One group of leads a run could be written for, by contact channel. */
export interface VisualGroup {
  key: string;
  label: string;
  count: number;
  /**
   * False for groups the brief cannot write for yet -- unchecked leads are
   * held at the same gate the site brief uses, so a button there would
   * promise work that cannot happen.
   */
  contactable: boolean;
  /** Capped: a count is the whole truth, sixty names is a wall. */
  capped: boolean;
  /**
   * These have had a message already. Still a batch worth choosing -- a
   * second approach with something new to say -- but never what "On
   * WhatsApp" should mean.
   */
  second_approach?: boolean;
  leads: { lead_id: string; name: string; category: string }[];
}

export interface VisualCandidates {
  enabled: boolean;
  total: number;
  groups: VisualGroup[];
}
