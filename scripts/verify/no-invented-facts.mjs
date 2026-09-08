/**
 * Refuse a front end that states something nobody here has checked.
 *
 * The page arrived from a model that writes plausible detail by default. It
 * shipped a made-up WhatsApp number as the connected account, a model name
 * that may not exist, per-lead prices nobody measured, a version string, and
 * a download link to software that has no download. Each reads as fact. On a
 * dashboard that spends money and messages real businesses, a convincing
 * guess is worse than a blank.
 *
 * This is a negative check, so it carries its own control: the same patterns
 * are run against a fixture that deliberately contains them, and this script
 * fails if that fixture comes back clean.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const REPO = path.resolve(import.meta.dirname, '..', '..');

const BANNED = [
  {
    pattern: /gemini-[0-9]+\.[0-9]+-\w+|claude-[0-9]|gpt-[0-9]|llama-?[0-9]/i,
    why: 'a model name written into the UI -- names move, and this one was never checked against an API',
  },
  {
    pattern: /\d\s*¢|\bcents? each\b|\$\s*\d+(\.\d+)?\s*(each|per)/i,
    why: 'a per-lead price nobody measured',
  },
  {
    pattern: /\+\s*1\s*\(\d{3}\)|\+60\d{6,}|\(\d{3}\)\s*\d{3}-\d{4}/,
    why: 'a phone number presented as real',
  },
  {
    pattern: /\bv\d+\.\d+\b/,
    why: 'a version string that matches nothing in this repository',
  },
  {
    pattern: /Download\s+(the\s+)?(Local\s+)?(Relay|helper)\s*(app)?/i,
    why: 'a download link for software that has no download -- the relay ships in this repository',
  },
  {
    pattern: /encrypt/i,
    why: 'a claim about encryption; keys are plain text in .env on the user\'s own machine',
  },
  {
    pattern: /127\.0\.0\.1:\d+/,
    why: 'a hardcoded relay address -- the port comes from the campaign file and the server reports it',
  },
];

const sources = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules') continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) walk(full);
    else if (/\.(tsx?|html|css)$/.test(entry)) sources.push(full);
  }
};
walk(path.join(REPO, 'frontend', 'src'));
sources.push(path.join(REPO, 'frontend', 'index.html'));

const scan = (text) => {
  const hits = [];
  for (const line of text.split('\n')) {
    // Comments explain why a pattern is banned and would otherwise match the
    // very words they forbid.
    const code = line.replace(/^\s*(\/\/|\*|\/\*).*$/, '');
    for (const { pattern, why } of BANNED) {
      const found = code.match(pattern);
      if (found) hits.push({ text: found[0], why });
    }
  }
  return hits;
};

const problems = [];
for (const file of sources) {
  for (const hit of scan(readFileSync(file, 'utf8'))) {
    problems.push(`${path.relative(REPO, file)}: ${JSON.stringify(hit.text)} -- ${hit.why}`);
  }
}

// The control. Every pattern must fire on text built to contain it, or a
// broken regex would report a clean page for ever.
const control = [
  'placeholder="gemini-3.8-flash"',
  "costHint: 'about 0.2¢ each'",
  'Connected: +1 (504) 555-0199',
  '<p>SME Pipeline v1.2</p>',
  'Download Local Relay app',
  'Stored encrypted server-side',
  'relay at 127.0.0.1:8787',
].join('\n');
const caught = new Set(scan(control).map((hit) => hit.why));
if (caught.size !== BANNED.length) {
  const missed = BANNED.filter((b) => !caught.has(b.why)).map((b) => b.why);
  console.error(`FAIL: control failed -- these patterns matched nothing: ${JSON.stringify(missed)}`);
  process.exit(1);
}

if (problems.length) {
  for (const problem of problems) console.error(`FAIL: ${problem}`);
  process.exit(1);
}
console.log('NO-INVENTED-FACTS');
