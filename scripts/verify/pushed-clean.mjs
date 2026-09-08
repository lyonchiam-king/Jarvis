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
const git = (...args) => spawnSync('git', args, { cwd: REPO, encoding: 'utf8' });

const problems = [];

const dirty = git('status', '--porcelain');
if (dirty.stdout.trim()) {
  problems.push(`uncommitted changes:\n${dirty.stdout.trim().split('\n').slice(0, 20).join('\n')}`);
}

const branch = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.trim();
const upstream = git('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}').stdout.trim();
if (!upstream) {
  problems.push(`${branch} tracks nothing -- push it with: git push -u origin ${branch}`);
} else {
  git('fetch', 'origin', branch);
  const local = git('rev-parse', 'HEAD').stdout.trim();
  const remote = git('rev-parse', upstream).stdout.trim();
  if (local !== remote) {
    const ahead = git('rev-list', '--count', `${upstream}..HEAD`).stdout.trim();
    const behind = git('rev-list', '--count', `HEAD..${upstream}`).stdout.trim();
    problems.push(
      `${branch} and ${upstream} differ: ${ahead} commit(s) not pushed, ` +
      `${behind} not pulled (local ${local.slice(0, 8)}, remote ${remote.slice(0, 8)})`
    );
  }
}
const local = git('rev-parse', 'HEAD').stdout.trim();

if (problems.length) {
  for (const problem of problems) console.error(`FAIL: ${problem}`);
  process.exit(1);
}
console.log(`PUSHED-CLEAN ${local.slice(0, 8)}`);
