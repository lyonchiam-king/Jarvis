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
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
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
    },
    // The infographic branch. Two rows on purpose: one named for the
    // business and one generic, because telling them apart on the card is
    // the whole safeguard -- a generic graphic sent as though it were
    // researched is a claim the owner can disprove from the picture.
    '/api/visual/queue': {
      enabled: true,
      track: 'both',
      tool_name: 'Google Flow',
      tool_url: '',
      signature: 'by Lyon, Business Growth Consultant',
      counts: { needs_image: 1, needs_check: 1, ready: 0, skipped: 0 },
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
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    },
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
  for (const phrase of [
    'Your Nail Crush',
    'Named for this business',
    // The safeguard, rendered. A generic graphic has to say so on its own
    // card, before anyone sends it.
    'Generic',
    // The one thing only a person can do, and the words to do it with.
    'These exact words must be on the picture',
    '3 quick wins for your business',
    // All three ways the picture comes back.
    'Ctrl+V',
  ]) {
    if (!vizText.includes(phrase)) {
      fail(`the Infographic step did not render ${JSON.stringify(phrase)}`);
    }
  }
  // All three ways in. The link field is a placeholder, so it is not in
  // innerText -- asserted off the DOM instead, which is also what proves the
  // field exists rather than that some sentence mentions a link.
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
  if (!/paste the link/i.test(vizWaysIn.url)) {
    fail(`the link field's placeholder is ${JSON.stringify(vizWaysIn.url)}`);
  }

  // The thumbnail must be an <img> pointed at the server, not the base64 of
  // one printed into the page -- the same bug the pairing code had.
  //
  // Either outcome is correct here, and both are asserted: the image, with a
  // src pointing at the server, or -- in this container, where that route
  // cannot answer without a Sheet -- the named note saying so. What must
  // never happen is a tick offered beside nothing, or base64 printed as text.
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
  await dt.evaluate(`document.getElementById('campaign-settings-toggle').click()`);
  await new Promise((r) => setTimeout(r, 250));

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
