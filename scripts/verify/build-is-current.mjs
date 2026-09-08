/**
 * Prove the committed bundle was built from the committed source.
 *
 * Lyon has no Node and never builds: what ships in src/outreach/web is what
 * his browser runs. So a change to frontend/ that is not rebuilt is not a
 * change at all -- the page keeps working, keeps looking right, and keeps
 * doing the old thing. Nothing else here would notice.
 *
 * Rebuilds into a scratch directory and compares file contents, so the check
 * never disturbs the committed output.
 */
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const SHIPPED = path.join(REPO, 'src', 'outreach', 'web');

const digestTree = (root) => {
  const out = new Map();
  const walk = (dir, prefix) => {
    for (const entry of readdirSync(dir).sort()) {
      const full = path.join(dir, entry);
      const rel = prefix ? `${prefix}/${entry}` : entry;
      if (statSync(full).isDirectory()) walk(full, rel);
      else out.set(rel, createHash('sha256').update(readFileSync(full)).digest('hex'));
    }
  };
  walk(root, '');
  return out;
};

const scratch = mkdtempSync(path.join(tmpdir(), 'outreach-build-'));
try {
  const build = spawnSync(
    'npx',
    ['vite', 'build', '--outDir', scratch, '--emptyOutDir'],
    { cwd: path.join(REPO, 'frontend'), encoding: 'utf8' },
  );
  if (build.status !== 0) {
    console.error(`FAIL: the front end does not build:\n${(build.stderr || build.stdout || '').slice(0, 2000)}`);
    process.exit(1);
  }

  const fresh = digestTree(scratch);
  const shipped = digestTree(SHIPPED);
  const problems = [];

  for (const [file, hash] of fresh) {
    if (!shipped.has(file)) problems.push(`${file} was built but is not committed`);
    else if (shipped.get(file) !== hash) problems.push(`${file} differs from a fresh build`);
  }
  for (const file of shipped.keys()) {
    if (!fresh.has(file)) problems.push(`${file} is committed but no longer built -- stale`);
  }

  if (problems.length) {
    for (const problem of problems) console.error(`FAIL: ${problem}`);
    console.error('Run `npm run build` in frontend/ and commit the result.');
    process.exit(1);
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
console.log('BUILD-IS-CURRENT');
