/**
 * Prove the front end is type-checked, and that the checker can fail.
 *
 * This gate exists because the answer was once yes and no at the same time:
 * `tsc --noEmit` exited 0 while `@types/react` was absent, which made every
 * component `any` and every property access unverifiable. A green typecheck
 * was cited as evidence the wiring was right. So this asserts three things --
 * the types are installed, `strict` is on, and a deliberate type error is
 * actually rejected. The third is the control: without it this script would
 * pass on a project where checking is silently disabled again.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const FRONTEND = path.resolve(import.meta.dirname, '..', '..', 'frontend');
const run = () => spawnSync('npx', ['tsc', '--noEmit'], { cwd: FRONTEND, encoding: 'utf8' });

const problems = [];

if (!existsSync(path.join(FRONTEND, 'node_modules', '@types', 'react'))) {
  problems.push('@types/react is not installed -- run `npm install` in frontend/');
}

const tsconfig = JSON.parse(readFileSync(path.join(FRONTEND, 'tsconfig.json'), 'utf8'));
if (tsconfig.compilerOptions?.strict !== true) problems.push('tsconfig has strict off');

const clean = run();
if (clean.status !== 0) {
  problems.push(`the front end does not typecheck:\n${(clean.stdout || '').slice(0, 2000)}`);
}

// The control. A checker that cannot reject anything is not a checker.
const probe = path.join(FRONTEND, 'src', '__typecheck_control__.ts');
try {
  writeFileSync(probe, 'export const control: number = "not a number";\n');
  const dirty = run();
  if (dirty.status === 0) {
    problems.push('control failed: a deliberate type error was accepted, so this gate proves nothing');
  }
} finally {
  rmSync(probe, { force: true });
}

if (problems.length) {
  for (const problem of problems) console.error(`FAIL: ${problem}`);
  process.exit(1);
}
console.log('TYPECHECK-IS-REAL');
