/**
 * Two rules this repository paid for, checked against the shipped bundle.
 *
 * Secrets are write-only: env_edit writes them, `is_set` reports presence,
 * and no key reaches a page, a log or a report. A front end with a "test
 * key" button is the obvious place for that to break, so the check reads the
 * built JavaScript -- what the browser is actually handed -- rather than the
 * source it came from.
 *
 * And a dialling code is never inferred in the browser. `phone_e164` is
 * normalised once, on the Python side, where the lead's own country is
 * known. Normalising a second time glued a country code onto a number that
 * already had one, and a UK lead went out as 60447450225230: a JID WhatsApp
 * accepts, reports as sent, and delivers to nobody. A US national number
 * opens with 504, which is Honduras, so guessing from digits is not a fix.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const problems = [];

const bundleFiles = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) walk(full);
    else if (/\.(js|css|html)$/.test(entry)) bundleFiles.push(full);
  }
};
walk(path.join(REPO, 'src', 'outreach', 'web'));
if (!bundleFiles.length) problems.push('there is no built bundle to inspect');

const bundle = bundleFiles.map((f) => readFileSync(f, 'utf8')).join('\n');

// Shapes of real credentials. None of these belongs in something served to
// a browser, and one baked in at build time would be readable by anyone on
// the network the dashboard is reachable from.
const CREDENTIAL_SHAPES = [
  [/AIza[0-9A-Za-z_-]{30,}/, 'a Google API key'],
  [/sk-[A-Za-z0-9_-]{20,}/, 'an OpenAI-style key'],
  [/sk-ant-[A-Za-z0-9_-]{20,}/, 'an Anthropic key'],
  [/ya29\.[0-9A-Za-z_-]{20,}/, 'a Google OAuth token'],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----/, 'a private key'],
];
for (const [pattern, what] of CREDENTIAL_SHAPES) {
  const found = bundle.match(pattern);
  if (found) problems.push(`the shipped bundle contains ${what}: ${found[0].slice(0, 12)}...`);
}

// The client may report whether a key is set; it may never read one back.
const client = readFileSync(path.join(REPO, 'frontend', 'src', 'api', 'client.ts'), 'utf8');
for (const field of ['api_key:', 'places_key:', 'model_key:']) {
  // Sending a key up is the whole point of the save button. Reading one
  // *down* is what must never appear -- so any mapping that pulls a key out
  // of a response is a failure.
  const readsBack = new RegExp(`(=|return)\\s*(d|data|res)\\.[a-z_]*${field.replace(':', '')}`);
  if (readsBack.test(client)) problems.push(`client.ts reads ${field} back from the server`);
}

const sources = [];
const walkSrc = (dir) => {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) walkSrc(full);
    else if (/\.tsx?$/.test(entry)) sources.push(full);
  }
};
walkSrc(path.join(REPO, 'frontend', 'src'));

const DIALLING = [
  [/['"`]\+\d{1,3}['"`]\s*\+/, 'a country code concatenated onto a number'],
  [/\+\s*['"`]\d{2,3}['"`]/, 'a country code prefixed to a number'],
  [/replace\([^)]*\/\^0\//, 'a leading zero stripped, which is a national-format rule'],
];
for (const file of sources) {
  const text = readFileSync(file, 'utf8');
  for (const line of text.split('\n')) {
    const code = line.replace(/^\s*(\/\/|\*|\/\*).*$/, '');
    for (const [pattern, what] of DIALLING) {
      if (pattern.test(code)) {
        problems.push(`${path.relative(REPO, file)}: ${what} -- normalising belongs in Python`);
      }
    }
  }
}

// The send must pass the number Python worked out, or the server falls back
// to normalising the display number with the campaign's country.
const sendQueue = readFileSync(
  path.join(REPO, 'frontend', 'src', 'views', 'SendQueueView.tsx'), 'utf8',
);
if (!sendQueue.includes('item.e164_phone')) {
  problems.push('SendQueueView no longer passes the dialled number back to the server');
}

// Controls, so a broken regex cannot report a clean result for ever.
const control = "const jid = '+60' + phone;\nconst key = 'AIzaSyDUMMYCONTROLVALUE0123456789abcd';";
const controlHits = [
  CREDENTIAL_SHAPES[0][0].test(control),
  DIALLING[0][0].test(control) || DIALLING[1][0].test(control),
];
if (!controlHits.every(Boolean)) {
  console.error(`FAIL: control failed -- the patterns did not fire on text built to trip them: ${JSON.stringify(controlHits)}`);
  process.exit(1);
}

if (problems.length) {
  for (const problem of problems) console.error(`FAIL: ${problem}`);
  process.exit(1);
}
console.log('SECRETS-AND-NUMBERS-OK');
