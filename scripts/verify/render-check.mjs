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
  const overflow = await dt.evaluate(`(() => {
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
  })()`);
  if (overflow.scrollWidth > overflow.innerWidth + 1) {
    fail(
      `the phone layout scrolls sideways: ${overflow.scrollWidth}px of content in ` +
      `${overflow.innerWidth}px`
    );
  }
  if (overflow.clipped.length) {
    fail(`content is cut off inside: ${overflow.clipped.join(', ')}`);
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
