/**
 * Load the React dashboard in a real browser and prove what it does.
 *
 * Everything else about this page is checked without running it: TypeScript
 * proves the types, Vite proves it bundles, pytest proves the routes and
 * assets answer. None of that notices a throw in the first render -- the
 * server logs two clean 200s and the person sees a white screen. This drives
 * Chromium over the DevTools protocol, so it can report four things a dump
 * of the HTML cannot:
 *
 *   - what React actually committed to the DOM,
 *   - every console error and every request that failed,
 *   - whether the phone layout scrolls sideways,
 *   - and a screenshot to look at.
 *
 * Usage: node scripts/verify/render-check.mjs [--shot <dir>]
 *
 * Prints RENDER-CHECK-PASS only after every assertion has passed, so the
 * token cannot appear on a partial run. Exits non-zero otherwise.
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs';
import { createConnection } from 'node:net';
import path from 'node:path';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const APP_PORT = 5057;
const CDP_PORT = 9333;
const PHONE = { width: 430, height: 932 };   // iPhone 16 Pro Max, points

// Text the React tree renders and the server never sends: index.html holds
// an empty <div id="root">, so finding these proves the bundle ran.
const MUST_RENDER = ['Outreach Studio', 'app-sidebar', 'nav-toggle-btn', 'top-app-header'];

// Sentences the generated page stated as fact and cannot support. Checked
// against what a browser shows rather than against the source, because the
// shipped bundle is what a person reads.
const FORBIDDEN = [
  '504) 555-0199',          // a made-up number shown as the linked account
  'Zero-Trust',             // keys are plain text in .env
  'copied to clipboard',    // nothing is ever copied anywhere
  'Encrypt Keys',
  'encrypted server-side',
  'gemini-3.8-flash',       // a model name nothing here has verified exists
];

const OVERFLOW_PROBE = `(() => {
    const doc = document.documentElement;
    const name = (el) =>
      el.id ? '#' + el.id : el.tagName.toLowerCase() + '.' + String(el.className).split(' ')[0];
    // Two different failures. The document scrolling sideways is the obvious
    // one; an element clipped inside a flex container is the one that
    // actually happened, and it leaves the document width untouched.
    const insideScroller = (el) => {
      for (let node = el.parentElement; node; node = node.parentElement) {
        // A box explicitly told to scroll sideways is doing its job: the
        // leads table is meant to be wider than the phone.
        if (node.className && String(node.className).includes('overflow-x-auto')) return true;
      }
      return false;
    };
    const clipped = [...document.querySelectorAll('body *')]
      .filter((el) => {
        if (!el.textContent || !el.textContent.trim()) return false;
        if (el.children.length) return false;         // leaves only, or every
        if (insideScroller(el)) return false;          // ancestor repeats it
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        return rect.right > window.innerWidth + 1;
      })
      .map((el) => name(el) + ' "' + el.textContent.trim().slice(0, 24) + '"')
      .slice(0, 6);
    return { scrollWidth: doc.scrollWidth, innerWidth: window.innerWidth, clipped };
  })()`;

const OPEN_FIRST_CARD = `(() => {
  const head = document.querySelector('[id^="visual-head-"]');
  if (!head) return false;
  if (head.getAttribute('aria-expanded') !== 'true') head.click();
  return true;
})()`;

const problems = [];
const fail = (message) => problems.push(message);

function chromiumBinary() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
  if (!existsSync(root)) return null;
  for (const dir of readdirSync(root)) {
    const candidate = path.join(root, dir, 'chrome-linux', 'chrome');
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

function waitForPort(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const socket = createConnection({ port, host: '127.0.0.1' });
      socket.once('connect', () => { socket.destroy(); resolve(); });
      socket.once('error', () => {
        socket.destroy();
        if (Date.now() > deadline) reject(new Error(`nothing listening on ${port}`));
        else setTimeout(attempt, 150);
      });
    };
    attempt();
  });
}

/** A minimal DevTools client. Node 22 ships WebSocket, so nothing is installed. */
class Devtools {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? reject(new Error(message.error.message)) : resolve(message.result);
      } else if (message.method) {
        this.events.push(message);
      }
    });
  }

  static async open(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', () => reject(new Error('CDP socket failed')), { once: true });
    });
    return new Devtools(socket);
  }

  send(method, params = {}) {
    const id = this.nextId++;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`${method} timed out`));
      }, 30000);
    });
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      // `text` is only ever "Uncaught", which costs an afternoon when the
      // throw is a null element three expressions deep. The exception's own
      // description carries the message and the stack.
      const detail = result.exceptionDetails;
      const described = detail.exception && detail.exception.description;
      throw new Error(String(described || detail.text).slice(0, 1200));
    }
    return result.result.value;
  }
}

const browser = chromiumBinary();
if (!browser) {
  console.error('FAIL: no Chromium under PLAYWRIGHT_BROWSERS_PATH');
  process.exit(1);
}

const server = spawn(
  path.join(REPO, '.venv', 'bin', 'python'),
  ['scripts/verify/serve_for_check.py', String(APP_PORT)],
  { cwd: REPO, stdio: ['ignore', 'ignore', 'pipe'] },
);
let serverErr = '';
server.stderr.on('data', (chunk) => { serverErr += chunk.toString(); });

const chrome = spawn(browser, [
  '--headless=new',
  '--disable-gpu',
  '--no-sandbox',
  '--hide-scrollbars',
  `--remote-debugging-port=${CDP_PORT}`,
  `--window-size=${PHONE.width},${PHONE.height}`,
  'about:blank',
], { stdio: ['ignore', 'ignore', 'ignore'] });

try {
  await waitForPort(APP_PORT, 25000);
  await waitForPort(CDP_PORT, 25000);

  const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
  const page = targets.find((t) => t.type === 'page');
  if (!page) throw new Error('Chromium opened no page target');

  const dt = await Devtools.open(page.webSocketDebuggerUrl);
  await dt.send('Runtime.enable');
  await dt.send('Log.enable');
  await dt.send('Network.enable');
  await dt.send('Page.enable');
  await dt.send('Emulation.setDeviceMetricsOverride', {
    width: PHONE.width,
    height: PHONE.height,
    deviceScaleFactor: 1,
    mobile: true,
  });

  await dt.send('Page.navigate', { url: `http://127.0.0.1:${APP_PORT}/app` });
  // React mounts after the bundle parses; poll rather than sleep a fixed
  // time, so a slow machine does not report a false empty page.
  const deadline = Date.now() + 20000;
  let html = '';
  while (Date.now() < deadline) {
    try {
      html = await dt.evaluate('document.getElementById("root")?.innerHTML || ""');
      if (html.length > 500) break;
    } catch { /* the page is still navigating */ }
    await new Promise((r) => setTimeout(r, 250));
  }

  const viewport = await dt.evaluate('window.innerWidth');
  if (viewport !== PHONE.width) {
    fail(`the viewport is ${viewport}px, not ${PHONE.width}px -- no phone measurement below is valid`);
  }

  if (html.length <= 500) fail(`#root stayed empty -- React never rendered (${html.length} bytes)`);
  for (const marker of MUST_RENDER) {
    if (!html.includes(marker)) fail(`React did not render ${JSON.stringify(marker)}`);
  }

  const visible = await dt.evaluate('document.body.innerText');
  const fullHtml = await dt.evaluate('document.documentElement.outerHTML');
  for (const phrase of FORBIDDEN) {
    if (`${visible}\n${fullHtml}`.includes(phrase)) {
      fail(`the page still shows ${JSON.stringify(phrase)}`);
    }
  }

  // Anything the browser logged as an error, and anything it could not load.
  // A page can render and still be broken; this is where that shows.
  const consoleErrors = dt.events
    .filter((e) => e.method === 'Log.entryAdded' && e.params.entry.level === 'error')
    .map((e) => e.params.entry.text);
  const failedRequests = dt.events
    .filter((e) => e.method === 'Network.loadingFailed')
    .map((e) => e.params.errorText);
  const badResponses = dt.events
    .filter((e) => e.method === 'Network.responseReceived' && e.params.response.status >= 400)
    .map((e) => `HTTP ${e.params.response.status} for ${e.params.response.url}`);
  for (const message of [...consoleErrors, ...failedRequests, ...badResponses]) {
    fail(`the browser reported: ${message}`);
  }

  // The layout Lyon will actually use. A page wider than the phone scrolls
  // sideways, which is how the setup panel lost its buttons off the right
  // edge -- visible in a screenshot, invisible to every other check here.
  const overflow = await dt.evaluate(OVERFLOW_PROBE);
  if (overflow.scrollWidth > overflow.innerWidth + 1) {
    fail(
      `the phone layout scrolls sideways: ${overflow.scrollWidth}px of content in ` +
      `${overflow.innerWidth}px`
    );
  }
  if (overflow.clipped.length) {
    fail(`content is cut off inside: ${overflow.clipped.join(', ')}`);
  }

  // ------------------------------------------------- the same page, with work in it
  //
  // Every count in this container is zero: there are no Sheet credentials
  // here, so the pass above only ever sees empty states. That left the
  // half of the interface that matters -- the recommendation, the queue,
  // the stage navigator carrying numbers -- rendered by nobody, ever.
  //
  // So the API is answered from a stub installed before the bundle runs.
  // This proves the views render real shapes and survive a phone width
  // with content in them. It proves nothing about whether Flask returns
  // these shapes; `client.ts` and the endpoint test own that question.
  const FIXTURE = {
    '/api/summary': {
      total: 24, disqualified: 3,
      by_stage: {
        Discovered: 5, Researched: 4, 'Site Briefed': 2, 'Site Built': 7,
        'Email Drafted': 0, Sent: 6, Replied: 2, Closed: 1, Disqualified: 3,
      },
      to_research: 5, to_brief: 2, to_draft: 3,
      awaiting_approval: 2, approved_unbuilt: 1, whatsapp_waiting: 4,
      emails_waiting: 0, errors: 2, errors_retryable: 1,
      sheet_url: 'https://docs.google.com/spreadsheets/d/EXAMPLE/edit',
      has_work: true,
    },
    '/api/log': { running: null, lines: ['discovery: 5 new leads', 'research: 4 done'] },
    '/api/leads': { leads: [] },
    // Shaped exactly like the relay's answer: a base64 PNG data URI. The
    // panel called it qrCodeSvg and injected it with innerHTML, so the
    // image data rendered as text in an empty white box.
    '/api/whatsapp/qr': {
      qr: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ'
        + 'AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    },
    // The send queue, with one message written for the pitch this campaign
    // no longer makes. Changing the track changes what is drafted next and
    // cannot reach back into the Sheet, so these are the messages that
    // actually go out -- a card carrying a demo-site link under an
    // infographic needs to say so before anyone presses Send.
    '/api/whatsapp/queue': {
      queue: [
        {
          lead_id: 'SEND-A', name: 'NS Aesthetics', phone: '+44 7356 003023',
          phone_e164: '+447356003023', has_shot: true, shot_kind: 'visual',
          message: 'We built a simple site. Website: https://example.com/ns',
          wa_link: 'https://wa.me/447356003023',
          stale_pitch: true,
        },
        {
          lead_id: 'SEND-B', name: 'Second Salon', phone: '+44 7356 003024',
          phone_e164: '+447356003024', has_shot: true, shot_kind: 'visual',
          message: 'Noticed people finding you and not getting in touch.',
          wa_link: 'https://wa.me/447356003024',
          stale_pitch: false,
        },
      ],
      excluded: [],
    },
    // The model settings, and the catalogue its button asks for. No model
    // name is written into Jarvis anywhere -- which left nowhere to look one
    // up the morning a provider retired the one this campaign named.
    // Longest path first: the stub matches by prefix in insertion order, so
    // '/api/model' listed above would answer for the catalogue too.
    '/api/model/catalogue': {
      provider: 'ollama-cloud',
      models: ['alpha-model:cloud', 'beta-model:cloud'],
    },
    '/api/model': {
      provider: 'ollama-cloud', model: 'retired-one:cloud', outreach_model: '',
      base_url: '', providers: ['anthropic', 'ollama-cloud', 'openrouter'],
      key_env: 'OLLAMA_API_KEY', key_needed: true, key_set: true,
      places_key_set: true, guaranteed: false,
    },
    '/api/campaign': {
      category: 'beauty salon', location: 'London, UK',
      areas: ['Soho', 'Shoreditch'], sender_name: 'Lyon',
      daily_batch_limit: 10, country_code: 'GB', config_file: 'example.yaml',
    },
    // The picker reads its own setting rather than the campaign's, so it
    // needs its own answer -- without one it falls back to the website
    // track and the signature field is correctly not asked for, which would
    // make the assertion below untestable rather than false.
    '/api/track': {
      track: 'both', tracks: ['both', 'visual', 'website'],
      signature: 'by Lyon, Business Growth Consultant',
      tool_name: 'Google Flow',
      tool_url: 'https://example.com/flow',
      tool_url_suggestion: 'https://example.com/suggested',
    },
    // Who a run would write for. Without this the picker renders its
    // "every lead already has a prompt" state and none of it is exercised.
    '/api/visual/candidates': {
      enabled: true,
      total: 63,
      groups: [
        { key: 'whatsapp', label: 'On WhatsApp, not messaged yet', count: 41, contactable: true,
          capped: true, leads: [
            { lead_id: 'W1', name: 'Salon One', category: 'Nail salon' },
            { lead_id: 'W2', name: 'Salon Two', category: 'Hair salon' }] },
        { key: 'email', label: 'Email only', count: 7, contactable: true,
          capped: false, leads: [
            { lead_id: 'E1', name: 'Clinic One', category: 'Clinic' }] },
        // Contacted before. Still a batch worth choosing, never what "On
        // WhatsApp" should mean.
        { key: 'messaged', label: 'Already messaged once', count: 23,
          contactable: true, capped: false, second_approach: true, leads: [] },
        { key: 'unchecked', label: 'Not checked yet', count: 15,
          contactable: false, capped: false, leads: [] },
        { key: 'none', label: 'No way to contact them', count: 0,
          contactable: false, capped: false, leads: [] },
      ],
    },
    // The infographic branch. Two rows on purpose: one named for the
    // business and one generic, because telling them apart on the card is
    // the whole safeguard -- a generic graphic sent as though it were
    // researched is a claim the owner can disprove from the picture.
    '/api/visual/queue': {
      enabled: true,
      track: 'both',
      tool_name: 'Google Flow',
      // Set, so the Open button must render. Blank it and the button is
      // supposed to disappear rather than become a dead link.
      tool_url: 'https://example.com/flow',
      signature: 'by Lyon, Business Growth Consultant',
      counts: { needs_image: 1, needs_check: 1, ready: 2, skipped: 0 },
      catalogue: {
        styles: ['paper-cut', 'watercolour'], layouts: ['rows', 'road'],
        chosen_styles: [], chosen_layouts: [],
      },
      queue: [
        {
          lead_id: 'LEAD-A', name: 'Your Nail Crush', category: 'Nail salon',
          prompt: 'Wide 16:9 landscape semi-infographic for "Your Nail Crush".',
          gaps: 'no_website: Google lists no website for this business',
          style: 'paper-cut', layout: 'rows', specific: true,
          state: 'needs_image', approval: 'Approved',
          expected_text: ['3 quick wins for Your Nail Crush'],
          message: '',
          sent_at: '',
          created_at: '2026-01-01T00:00:00Z',
        },
        {
          lead_id: 'LEAD-B', name: 'Second Salon', category: 'Hair salon',
          prompt: 'Wide 16:9 landscape semi-infographic for a small local business.',
          gaps: 'generic_manual: Generic graphic -- not a claim about this business',
          style: 'watercolour', layout: 'road', specific: false,
          state: 'needs_check', approval: 'Approved',
          expected_text: ['3 quick wins for your business',
                          'Time going on jobs a tool could do  ->  Automate the repeated ones'],
          // The real message, so the block answering "is THIS what gets
          // sent?" is actually rendered rather than taking its empty branch.
          message: '',
          sent_at: '',
          created_at: '2026-01-01T00:00:00Z',
        },
        {
          lead_id: 'LEAD-C', name: 'Third Salon', category: 'Hair salon',
          prompt: 'Wide 16:9 landscape semi-infographic for "Third Salon".',
          gaps: 'no_website: Google lists no website for this business',
          style: 'flat-vector', layout: 'rows', specific: true,
          state: 'ready', approval: 'Approved',
          expected_text: ['3 quick wins for Third Salon'],
          // The real message, so the block answering "is THIS what gets
          // sent?" renders rather than taking its empty branch.
          message: 'Hi, Lyon here. Noticed people asking how to book.',
          sent_at: '',
          created_at: '2026-01-01T00:00:00Z',
        },
        {
          // Messaged months ago with the website pitch, and now holding a
          // confirmed picture. The draft stage will not touch a lead that
          // already has an Outreach Log row, so without the redraft button
          // this graphic could never get a message of its own.
          lead_id: 'LEAD-D', name: 'Fourth Salon', category: 'Hair salon',
          prompt: 'Wide 16:9 landscape semi-infographic for "Fourth Salon".',
          gaps: 'no_website: Google lists no website for this business',
          style: 'paper-cut', layout: 'road', specific: true,
          state: 'ready', approval: 'Approved',
          expected_text: ['3 quick wins for Fourth Salon'],
          message: 'Hi, Lyon here. I built you a page to look at.',
          sent_at: '2026-08-28 09:14',
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    },
    // The redraft itself. The stub answers {} for an unlisted path, which
    // the view would read as success -- named here so the button is driven
    // against a reply this check chose.
    '/api/visual/redraft': { ok: true, lead_id: 'LEAD-D' },
  };

  await dt.send('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => {
      const fixture = ${JSON.stringify(FIXTURE)};
      window.fetch = async (input) => {
        const url = typeof input === 'string' ? input : input.url;
        const key = Object.keys(fixture).find((k) => url.startsWith(k));
        return new Response(JSON.stringify(key ? fixture[key] : {}), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      };
    })()`,
  });

  dt.events.length = 0;
  await dt.send('Page.navigate', { url: `http://127.0.0.1:${APP_PORT}/app` });

  const withWork = Date.now() + 20000;
  let loaded = '';
  while (Date.now() < withWork) {
    loaded = await dt.evaluate('document.body.innerText || ""');
    if (loaded.includes('Ready to contact')) break;
    await new Promise((r) => setTimeout(r, 250));
  }

  // Today: a recommendation, the honest tiles, and the queue -- each with
  // the real numbers from the fixture rather than a placeholder.
  for (const phrase of [
    'Review and send 3 messages',   // the recommendation, chosen by state
    'Ready to contact',
    'Not tracked',                   // follow-ups, said plainly
    'Waiting on you',
    'Messages to review and send',
  ]) {
    if (!loaded.includes(phrase)) {
      fail(`Today with data did not render ${JSON.stringify(phrase)}`);
    }
  }

  // The queue must carry the counts, not just the labels.
  const queueCounts = await dt.evaluate(
    `[...document.querySelectorAll('#task-send, #task-build, #task-brief')]
       .map(el => el.innerText.replace(/\\s+/g, ' ').trim())`,
  );
  if (!Array.isArray(queueCounts) || queueCounts.length !== 3) {
    fail(`the task queue rendered ${JSON.stringify(queueCounts)}, expected three rows`);
  }

  // The stage navigator, reached the way Today reaches it.
  await dt.evaluate(`document.getElementById('today-primary-action').click()`);
  await new Promise((r) => setTimeout(r, 400));
  const stageText = await dt.evaluate('document.body.innerText || ""');
  for (const step of ['List', 'Reachability', 'Brief', 'Build', 'Review & Send']) {
    if (!stageText.includes(step)) fail(`the stage navigator is missing ${JSON.stringify(step)}`);
  }
  const onSendStep = await dt.evaluate(
    `document.getElementById('stage-send')?.getAttribute('aria-current') === 'step'`,
  );
  if (!onSendStep) {
    fail('Today\'s recommendation did not land on the Review & Send step');
  }

  // The infographic branch. It only appears when the campaign asks for
  // graphics, so without the fixture above it would never render at all --
  // which is exactly how a stage with no button once shipped and never ran.
  if (!stageText.includes('Infographic')) {
    fail('the stage navigator is missing the Infographic branch');
  }
  await dt.evaluate(`document.getElementById('stage-visual').click()`);
  await new Promise((r) => setTimeout(r, 500));
  const vizText = await dt.evaluate('document.body.innerText || ""');
  // The picker, which is what keeps sixty prompts from being written in one
  // press. Groups first, because that is the split he asked to work from.
  for (const phrase of ['Who to write for', 'On WhatsApp', 'Email only',
                        'Already messaged once', 'second approach',
                        'Not checked yet']) {
    if (!vizText.includes(phrase)) {
      fail(`the Infographic step did not render ${JSON.stringify(phrase)}`);
    }
  }
  const whoFor = await dt.evaluate(`(() => {
    const run = document.getElementById('visual-run-whatsapp');
    const held = document.getElementById('visual-run-unchecked');
    const limit = document.getElementById('visual-limit');
    return {
      group: !!run, heldHasButton: !!held,
      limit: limit ? limit.value : null,
      ticked: document.getElementById('visual-picked-note')?.textContent || '',
    };
  })()`);
  if (!whoFor.group) fail('no way to run for the WhatsApp group');
  if (whoFor.heldHasButton) {
    // These are held at the same gate the site brief uses, so a button
    // would promise work that cannot happen.
    fail('a group nothing can be written for is offering a run button');
  }
  if (whoFor.limit !== '10') {
    fail(`the How many box defaults to ${JSON.stringify(whoFor.limit)}, not 10`);
  }
  if (!/Nothing ticked/.test(whoFor.ticked)) {
    fail(`the tick count reads ${JSON.stringify(whoFor.ticked)}`);
  }

  // Cards are collapsed until asked for. Sixty expanded cards is the thing
  // this list was reported for.
  const collapsed = await dt.evaluate(`(() => {
    const body = document.body.innerText || '';
    return {
      rows: document.querySelectorAll('[id^="visual-head-"]').length,
      expanded: /Copy prompt for/.test(body),
      // The default list is the only state that needs him.
      active: document.getElementById('visual-filter-needs_image')
        ?.className.includes('emerald') || false,
    };
  })()`);
  if (!collapsed.rows) fail('the Infographic step rendered no cards at all');
  if (collapsed.expanded) {
    fail('cards are expanded before anyone asked -- the list is a wall again');
  }
  if (!collapsed.active) fail('the list does not open on "Need a picture"');

  // The default list holds the lead waiting for a picture. Opening it shows
  // the three ways the picture comes back, and the way to get it off the list.
  await dt.evaluate(OPEN_FIRST_CARD);
  await new Promise((r) => setTimeout(r, 300));
  const openedWaiting = await dt.evaluate('document.body.innerText || ""');
  for (const phrase of ['Your Nail Crush', 'Named', 'Ctrl+V', 'Remove']) {
    if (!openedWaiting.includes(phrase)) {
      fail(`the opened waiting card did not render ${JSON.stringify(phrase)}`);
    }
  }

  // The other list: a picture back, unchecked, and generic. Both safeguards
  // live on that one -- the words to read against it, and the badge saying it
  // claims nothing about this business.
  await dt.evaluate(`document.getElementById('visual-filter-needs_check').click()`);
  await new Promise((r) => setTimeout(r, 300));
  await dt.evaluate(OPEN_FIRST_CARD);
  await new Promise((r) => setTimeout(r, 400));
  const vizText2 = await dt.evaluate('document.body.innerText || ""');
  for (const phrase of [
    'Second Salon',
    'Generic',
    'not enough evidence to name them',
    'Proofread the picture',
    '3 quick wins for your business',
    // Asked after a real run, about this very box: "are these going to be the
    // messages that are going to be sent out?" It has to say no.
    'not the message',
  ]) {
    if (!vizText2.includes(phrase)) {
      fail(`an opened card did not render ${JSON.stringify(phrase)}`);
    }
  }
  // The checked card: what actually gets sent, and where from. A card that
  // says "ready to send" and offers no onward signpost is a dead end.
  await dt.evaluate(`document.getElementById('visual-filter-ready').click()`);
  await new Promise((r) => setTimeout(r, 300));
  await dt.evaluate(OPEN_FIRST_CARD);
  await new Promise((r) => setTimeout(r, 400));
  const checked = await dt.evaluate('document.body.innerText || ""');
  for (const phrase of [
    'Third Salon',
    'The message this picture goes out with',
    'Hi, Lyon here. Noticed people asking how to book.',
    'no separate send button here',
  ]) {
    if (!checked.includes(phrase)) {
      fail(`the checked card did not render ${JSON.stringify(phrase)}`);
    }
  }

  // The lead already messaged with the website pitch. Reported from a real
  // run: a card saying "Already sent" over a message about a website, and no
  // way to write the one about the picture.
  const sentOpened = await dt.evaluate(`(() => {
    const head = document.getElementById('visual-head-LEAD-D');
    if (!head) return false;
    if (head.getAttribute('aria-expanded') !== 'true') head.click();
    return true;
  })()`);
  if (!sentOpened) fail('the already-messaged lead rendered no card');
  await new Promise((r) => setTimeout(r, 400));
  const sentText = await dt.evaluate('document.body.innerText || ""');
  for (const phrase of [
    'Fourth Salon',
    'Already messaged on 2026-08-28 09:14',
    'Hi, Lyon here. I built you a page to look at.',
    'Write a new message for this picture',
  ]) {
    if (!sentText.includes(phrase)) {
      fail(`the already-messaged card did not render ${JSON.stringify(phrase)}`);
    }
  }
  // Pressing it is the point: the button existed, was typed, built and
  // shipped once before as a thing no page rendered.
  await dt.evaluate(`document.getElementById('visual-redraft-LEAD-D').click()`);
  await new Promise((r) => setTimeout(r, 500));
  const redrafted = await dt.evaluate(`(() => {
    const card = document.getElementById('visual-sent-LEAD-D');
    const button = document.getElementById('visual-redraft-LEAD-D');
    return {
      text: card ? (card.innerText || '').replace(/\\s+/g, ' ') : '',
      disabled: button ? button.disabled : null,
    };
  })()`);
  if (!/back in Review/.test(redrafted.text)) {
    fail(`pressing redraft left the card saying ${JSON.stringify(redrafted.text)}`);
  }
  if (redrafted.disabled !== true) {
    // A second press would rewrite the message he has just been told is
    // waiting in the send queue.
    fail('the redraft button is still pressable after it has run');
  }

  // All three ways in. Read off the waiting card, which is the only state
  // that offers them. The link field is a placeholder, so it is not in
  // innerText -- asserted off the DOM instead, which is also what proves the
  // field exists rather than that some sentence mentions a link.
  await dt.evaluate(`document.getElementById('visual-filter-needs_image').click()`);
  await new Promise((r) => setTimeout(r, 250));
  await dt.evaluate(OPEN_FIRST_CARD);
  await new Promise((r) => setTimeout(r, 300));
  const vizWaysIn = await dt.evaluate(`(() => {
    const drop = document.querySelector('[id^="visual-drop-"]');
    const url = document.querySelector('[id^="visual-item-"] input[type="url"]');
    const file = document.querySelector('[id^="visual-item-"] input[type="file"]');
    return {
      drop: !!drop, file: !!file,
      url: url ? String(url.getAttribute('placeholder') || '') : '',
    };
  })()`);
  if (!vizWaysIn.drop) fail('the Infographic step offers no paste/drop target');
  if (!vizWaysIn.file) fail('the Infographic step offers no file picker');
  if (!/copy image address/i.test(vizWaysIn.url)) {
    fail(
      `the link field does not say which address to copy: ` +
      `${JSON.stringify(vizWaysIn.url)}`
    );
  }

  // The thumbnail must be an <img> pointed at the server, not the base64 of
  // one printed into the page -- the same bug the pairing code had.
  //
  // Either outcome is correct here, and both are asserted: the image, with a
  // src pointing at the server, or -- in this container, where that route
  // cannot answer without a Sheet -- the named note saying so. What must
  // never happen is a tick offered beside nothing, or base64 printed as text.
  // Back to the checked card for the thumbnail.
  await dt.evaluate(`document.getElementById('visual-filter-needs_check').click()`);
  await new Promise((r) => setTimeout(r, 250));
  await dt.evaluate(OPEN_FIRST_CARD);
  await new Promise((r) => setTimeout(r, 400));
  const vizShot = await dt.evaluate(`(() => {
    const img = document.querySelector('[id^="visual-item-"] img');
    const note = document.querySelector('[id^="visual-noimage-"]');
    return {
      has: !!img,
      src: img ? String(img.getAttribute('src')).slice(0, 20) : '',
      explained: !!note,
      leaked: /base64,[A-Za-z0-9+/]{16}/.test(document.body.innerText || ''),
    };
  })()`);
  if (vizShot.has) {
    if (!vizShot.src.startsWith('/api/screenshot/')) {
      fail(`the picture src is ${JSON.stringify(vizShot.src)}, not a server path`);
    }
  } else if (!vizShot.explained) {
    fail('the card shows neither the picture nor a reason it is missing');
  }
  if (vizShot.leaked) {
    fail('raw base64 image data is being shown as text on the Infographic step');
  }
  // The Open button. He asked for one press from the card to the image tool,
  // and the address is a setting rather than a constant, so this proves the
  // setting reaches the button.
  const openButton = await dt.evaluate(`(() => {
    const a = document.getElementById('visual-open-tool');
    return { has: !!a, href: a ? a.getAttribute('href') : '',
             label: a ? (a.textContent || '').trim() : '' };
  })()`);
  if (!openButton.has) {
    fail('the Infographic step has no button through to the image tool');
  } else {
    if (openButton.href !== 'https://example.com/flow') {
      fail(`the Open button points at ${JSON.stringify(openButton.href)}`);
    }
    if (!/Google Flow/.test(openButton.label)) {
      fail(`the Open button is labelled ${JSON.stringify(openButton.label)}`);
    }
  }

  // The rotation picker. Narrowing the series is the one thing he asked for
  // by name, and an endpoint with no control is a feature nobody has -- the
  // same shape as the click-to-chat link that was computed, returned, and
  // drawn by neither page.
  const rotation = await dt.evaluate(`(() => {
    const toggle = document.getElementById('rotation-toggle');
    if (!toggle) return { toggle: false };
    toggle.click();
    return { toggle: true };
  })()`);
  if (!rotation.toggle) {
    fail('the Infographic step offers no way to narrow the rotation');
  } else {
    await new Promise((r) => setTimeout(r, 300));
    const boxes = await dt.evaluate(`(() => {
      const names = (group) =>
        [...document.querySelectorAll('input[data-rotation="' + group + '"]')]
          .map((el) => el.value);
      return {
        styles: names('styles'),
        layouts: names('layouts'),
        save: !!document.getElementById('save-rotation-btn'),
      };
    })()`);
    // Drawn from the fixture's catalogue, not from a list in the page: a
    // hardcoded one drifts from what the server will accept and the mismatch
    // only shows as a refused save.
    if (!boxes.styles.includes('paper-cut') || !boxes.styles.includes('watercolour')) {
      fail(`the rotation styles rendered ${JSON.stringify(boxes.styles)}`);
    }
    if (!boxes.layouts.includes('road')) {
      fail(`the rotation layouts rendered ${JSON.stringify(boxes.layouts)}`);
    }
    if (!boxes.save) fail('the rotation picker has no save button');
  }

  // Phone width, with the prompt and the words on screen. The prompt is the
  // longest unbroken text anywhere in this interface.
  const vizOverflow = await dt.evaluate(OVERFLOW_PROBE);
  if (vizOverflow.scrollWidth > vizOverflow.innerWidth + 1) {
    fail(
      `the Infographic step scrolls sideways on a phone: ` +
      `${vizOverflow.scrollWidth}px in ${vizOverflow.innerWidth}px`
    );
  }
  if (vizOverflow.clipped.length) {
    fail(`the Infographic step clips content: ${vizOverflow.clipped.join(', ')}`);
  }

  // The track picker, in campaign settings. It is the control that switches
  // graphics on, so it has to be reachable from a campaign that has them off
  // -- which is why it does not live inside the step it enables.
  await dt.evaluate(`document.getElementById('campaign-settings-toggle').click()`);
  await new Promise((r) => setTimeout(r, 400));
  const picker = await dt.evaluate(`(() => {
    const el = document.getElementById('track-picker');
    if (!el) return { has: false };
    const chosen = [...el.querySelectorAll('input[type="radio"]')]
      .map((r) => r.value);
    return {
      has: true,
      options: chosen,
      // "both" is the fixture's track, so the signature field must be asked
      // for: a graphics campaign with nothing to sign them will not load.
      signature: !!document.getElementById('track-signature'),
      save: !!document.getElementById('save-track-btn'),
    };
  })()`);
  if (!picker.has) {
    fail('campaign settings offers no way to choose what the message pitches');
  } else {
    for (const option of ['website', 'visual', 'both']) {
      if (!picker.options.includes(option)) {
        fail(`the track picker is missing ${JSON.stringify(option)}`);
      }
    }
    if (!picker.signature) {
      fail('the track picker does not ask for the signature a graphic needs');
    }
    if (!picker.save) fail('the track picker has no save button');
  }
  // The address field, with what is saved in it -- not the suggestion, which
  // is only offered while nothing has been saved. Getting that the wrong way
  // round would quietly overwrite a corrected address with a guess on the
  // next Save.
  const toolFields = await dt.evaluate(`(() => {
    const url = document.getElementById('tool-url');
    const name = document.getElementById('tool-name');
    return { url: url ? url.value : null, name: name ? name.value : null };
  })()`);
  if (toolFields.url === null || toolFields.name === null) {
    fail('campaign settings offers no field for the image tool address');
  } else {
    if (toolFields.url !== 'https://example.com/flow') {
      fail(`the saved tool address rendered as ${JSON.stringify(toolFields.url)}`);
    }
    if (toolFields.name !== 'Google Flow') {
      fail(`the tool name rendered as ${JSON.stringify(toolFields.name)}`);
    }
  }
  await dt.evaluate(`document.getElementById('campaign-settings-toggle').click()`);
  await new Promise((r) => setTimeout(r, 250));

  // Review & Send, where both tracks end. The warning on a message still
  // selling the page is the last thing between a business that has already
  // read that pitch and reading it a second time.
  await dt.evaluate(`document.getElementById('stage-send').click()`);
  await new Promise((r) => setTimeout(r, 700));
  const sendStep = await dt.evaluate(`(() => {
    const warn = document.getElementById('send-stale-pitch-SEND-A');
    return {
      body: (document.body.innerText || '').replace(/\\s+/g, ' '),
      warned: warn ? (warn.innerText || '').replace(/\\s+/g, ' ') : '',
      // The control: the card beside it was rewritten and must stay quiet.
      quiet: !document.getElementById('send-stale-pitch-SEND-B'),
    };
  })()`);
  if (!sendStep.body.includes('NS Aesthetics')) {
    fail('Review & Send rendered no queue at all');
  }
  if (!/written for the website pitch/.test(sendStep.warned)) {
    fail(`the stale-pitch card did not warn: ${JSON.stringify(sendStep.warned)}`);
  }
  if (!sendStep.quiet) {
    fail('a rewritten message was flagged too -- a warning on everything is read by nobody');
  }
  if (!sendStep.body.includes('Infographic attached')) {
    fail('Review & Send did not name which picture it is carrying');
  }

  // The missing-tab repair. Every schema addition breaks every existing
  // Sheet the same way, so the button beside that error is the difference
  // between one press and a terminal command the owner cannot run. Driven
  // by swapping the fixture's answer for the failure a real Sheet gave.
  await dt.evaluate(`(() => {
    const original = window.fetch;
    window.fetch = async (input) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.startsWith('/api/visual/queue')) {
        return new Response(JSON.stringify({
          error: 'Your Sheet has no "Visual Briefs" tab yet.\\nPress the button below.',
          missing_tab: true,
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return original(input);
    };
  })()`);
  await dt.evaluate(`document.getElementById('stage-send').click()`);
  await new Promise((r) => setTimeout(r, 250));
  await dt.evaluate(`document.getElementById('stage-visual').click()`);
  await new Promise((r) => setTimeout(r, 800));
  const repair = await dt.evaluate(`(() => {
    const box = document.getElementById('visual-error');
    const btn = document.getElementById('repair-tabs-btn');
    return {
      shown: !!box,
      text: box ? (box.innerText || '') : '',
      button: !!btn,
      // The message runs to several lines and the repair is the last of
      // them, so collapsed newlines would run the steps together.
      wraps: box ? getComputedStyle(box.querySelector('p')).whiteSpace : '',
    };
  })()`);
  if (!repair.shown) fail('the missing-tab error did not render at all');
  if (!repair.button) {
    fail('the missing-tab error offers no button, so the only fix is a terminal');
  }
  if (!/Visual Briefs/.test(repair.text)) {
    fail(`the missing-tab error does not name the tab: ${JSON.stringify(repair.text)}`);
  }
  if (!/pre-wrap|pre-line/.test(repair.wraps)) {
    fail(`the multi-line message is collapsed (white-space: ${repair.wraps})`);
  }

  // Settings: the message model, and the one control that answers "which
  // model should I use now". Both arrived as fields the client read and
  // nothing rendered, so neither could be changed from this page at all.
  await dt.evaluate(`document.getElementById('nav-item-settings').click()`);
  await new Promise((r) => setTimeout(r, 700));
  const settings = await dt.evaluate(`(() => {
    const message = document.getElementById('settings-outreach-model');
    const button = document.getElementById('settings-catalogue-btn');
    return {
      hasMessageModel: !!message,
      hasButton: !!button,
      body: (document.body.innerText || '').replace(/\\s+/g, ' '),
    };
  })()`);
  if (!settings.hasMessageModel) {
    fail('Settings offers no message-model field, so it can only be changed elsewhere');
  }
  if (!settings.hasButton) {
    fail('Settings offers no way to ask which models the key has');
  }
  if (!/the website ones and the infographic ones alike/.test(settings.body)) {
    fail('Settings does not say the message model writes both kinds of message');
  }

  await dt.evaluate(`document.getElementById('settings-catalogue-btn').click()`);
  await new Promise((r) => setTimeout(r, 600));
  // Clicked in its own step: React commits on the next render, so reading
  // the input in the same expression reads the value from before the click.
  const clicked = await dt.evaluate(`(() => {
    const list = document.getElementById('settings-catalogue');
    const use = document.getElementById('settings-pick-model-alpha-model:cloud');
    const forMessages = document.getElementById('settings-pick-message-beta-model:cloud');
    if (use) use.click();
    if (forMessages) forMessages.click();
    return {
      listed: list ? (list.innerText || '').replace(/\\s+/g, ' ') : '',
      found: !!use && !!forMessages,
    };
  })()`);
  await new Promise((r) => setTimeout(r, 400));
  const picked = await dt.evaluate(`(() => {
    const model = document.getElementById('settings-model');
    const message = document.getElementById('settings-outreach-model');
    return {
      model: model ? model.value : '',
      message: message ? message.value : '',
    };
  })()`);
  picked.listed = clicked.listed;
  if (!clicked.found) fail('the catalogue rendered no pick buttons');
  if (!/alpha-model:cloud/.test(picked.listed) || !/beta-model:cloud/.test(picked.listed)) {
    fail(`the catalogue listed ${JSON.stringify(picked.listed)}`);
  }
  // Clicking has to fill the box: retyping a name read off a screen is how a
  // typo becomes a 404 that looks exactly like a retired model.
  if (picked.model !== 'alpha-model:cloud') {
    fail(`Use did not fill the model box -- it reads ${JSON.stringify(picked.model)}`);
  }
  if (picked.message !== 'beta-model:cloud') {
    fail(`For messages did not fill its box -- it reads ${JSON.stringify(picked.message)}`);
  }

  // Results, which is new and entirely derived from the summary.
  await dt.evaluate(`document.getElementById('nav-item-results').click()`);
  await new Promise((r) => setTimeout(r, 400));
  const resultsText = await dt.evaluate('document.body.innerText || ""');
  for (const phrase of ['Messages sent', 'Where everyone currently sits', 'What is not here']) {
    if (!resultsText.includes(phrase)) {
      fail(`Results did not render ${JSON.stringify(phrase)}`);
    }
  }

  // The pairing QR must arrive as an image, not as its own base64 printed
  // into the page. Nothing else here would notice: the panel renders, the
  // request succeeds, and what the reader gets is a blank white square with
  // a few characters of PNG beside it.
  await dt.evaluate(`document.getElementById('nav-item-today').click()`);
  await new Promise((r) => setTimeout(r, 250));
  const qrButton = await dt.evaluate(
    `(() => {
       const b = [...document.querySelectorAll('button')]
         .find(el => /Show WhatsApp QR/i.test(el.textContent || ''));
       if (!b) return false;
       b.click();
       return true;
     })()`,
  );
  if (!qrButton) {
    fail('the setup panel offers no way to show the WhatsApp pairing code');
  } else {
    await new Promise((r) => setTimeout(r, 600));
    const qr = await dt.evaluate(`(() => {
      const img = document.querySelector('img[alt*="pairing" i]');
      return {
        hasImage: !!img,
        src: img ? String(img.getAttribute('src')).slice(0, 24) : '',
        leaked: (document.body.innerText || '').includes('iVBORw0KGgo'),
      };
    })()`);
    if (!qr.hasImage) fail('the pairing code did not render as an image');
    if (qr.hasImage && !qr.src.startsWith('data:image/png')) {
      fail(`the pairing image src is ${JSON.stringify(qr.src)}, not a PNG`);
    }
    if (qr.leaked) fail('raw base64 image data is being shown as text on the page');
  }

  // And it still fits a phone with all of that on screen.
  const loadedOverflow = await dt.evaluate(OVERFLOW_PROBE);
  if (loadedOverflow.scrollWidth > loadedOverflow.innerWidth + 1) {
    fail(
      `with data, the phone layout scrolls sideways: ${loadedOverflow.scrollWidth}px ` +
      `in ${loadedOverflow.innerWidth}px`
    );
  }
  if (loadedOverflow.clipped.length) {
    fail(`with data, content is cut off inside: ${loadedOverflow.clipped.join(', ')}`);
  }

  const loadedErrors = dt.events
    .filter((e) => e.method === 'Log.entryAdded' && e.params.entry.level === 'error')
    // The fetch stub above cannot intercept an <img src>, and
    // /api/screenshot/ is the one route serving a file rather than JSON -- so
    // in this container, which has no Sheet credentials, it answers 502 and
    // the browser logs it. That is the container's state, not a defect in
    // the page: the card has an onError path and says so instead of showing
    // a broken image, which is asserted above. Matched on the entry's own
    // url, not on its message, so this exempts that one request and nothing
    // else that happens to fail the same way.
    .filter((e) => !String(e.params.entry.url || '').includes('/api/screenshot/'))
    .map((e) => e.params.entry.text);
  for (const message of loadedErrors) {
    fail(`with data, the browser reported: ${message}`);
  }

  // A control: the same assertions run against the old page must NOT pass.
  // Without this the check would certify any page that happened to load.
  await dt.send('Page.navigate', { url: `http://127.0.0.1:${APP_PORT}/` });
  await new Promise((r) => setTimeout(r, 1500));
  const oldPage = await dt.evaluate('document.documentElement.outerHTML');
  if (oldPage.includes('app-sidebar')) {
    fail('control failed: the old page matched too, so this check proves nothing');
  }

  const shotIndex = process.argv.indexOf('--shot');
  if (shotIndex !== -1 && process.argv[shotIndex + 1]) {
    const dir = path.resolve(process.argv[shotIndex + 1]);
    mkdirSync(dir, { recursive: true });
    await dt.send('Page.navigate', { url: `http://127.0.0.1:${APP_PORT}/app` });
    await new Promise((r) => setTimeout(r, 2500));
    const { data } = await dt.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    const file = path.join(dir, 'app-phone.png');
    writeFileSync(file, Buffer.from(data, 'base64'));
    // A zero exit is not proof the browser wrote anything; bytes are.
    if (!existsSync(file)) fail('no screenshot was written');
    else console.error(`(screenshot: ${file})`);
  }
} catch (error) {
  fail(`${error.message}${serverErr ? ` -- server said: ${serverErr.slice(0, 300)}` : ''}`);
} finally {
  chrome.kill('SIGTERM');
  server.kill('SIGTERM');
}

if (problems.length) {
  for (const problem of problems) console.error(`FAIL: ${problem}`);
  process.exit(1);
}
console.log('RENDER-CHECK-PASS');
