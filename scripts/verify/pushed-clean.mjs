/**
 * Prove the work exists somewhere other than this container.
 *
 * This machine is reclaimed when the session ends. Work that is committed
 * but not pushed, or edited but not committed, is work Lyon cannot pull --
 * and "done" would be true only here.
 */
import { spawnSync } from 'node:child_process';
import path from 'node:path';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const BRANCH = 'claude/sme-outreach-pipeline-crn9f1';
const git = (...args) => spawnSync('git', args, { cwd: REPO, encoding: 'utf8' });

const problems = [];

const dirty = git('status', '--porcelain');
if (dirty.stdout.trim()) {
  problems.push(`uncommitted changes:\n${dirty.stdout.trim().split('\n').slice(0, 20).join('\n')}`);
}

const branch = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.trim();
if (branch !== BRANCH) problems.push(`on ${branch}, not ${BRANCH}`);

git('fetch', 'origin', BRANCH);
const local = git('rev-parse', 'HEAD').stdout.trim();
const remote = git('rev-parse', `origin/${BRANCH}`).stdout.trim();
if (!remote) problems.push(`origin/${BRANCH} does not exist`);
else if (local !== remote) {
  const ahead = git('rev-list', '--count', `origin/${BRANCH}..HEAD`).stdout.trim();
  problems.push(`${ahead} commit(s) not pushed (local ${local.slice(0, 8)}, remote ${remote.slice(0, 8)})`);
}

if (problems.length) {
  for (const problem of problems) console.error(`FAIL: ${problem}`);
  process.exit(1);
}
console.log(`PUSHED-CLEAN ${local.slice(0, 8)}`);
