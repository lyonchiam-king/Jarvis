/**
 * Run the tests that check the seam between the page and the dashboard.
 *
 * A separate script rather than a shell pipeline because "passed" appears in
 * pytest's failure line too ("1 failed, 20 passed"), so a gate matching that
 * word would certify a red run.
 */
import { spawnSync } from 'node:child_process';

const result = spawnSync(
  '.venv/bin/pytest',
  ['tests/test_spa_dashboard.py', '-q'],
  { encoding: 'utf8', cwd: new URL('../..', import.meta.url).pathname },
);
const output = `${result.stdout || ''}${result.stderr || ''}`;

if (result.status !== 0) {
  console.error(output.slice(-3000));
  process.exit(1);
}
const passed = output.match(/(\d+) passed/);
if (!passed || /failed|error/i.test(output)) {
  console.error(output.slice(-3000));
  process.exit(1);
}
console.log(`WIRING-TESTS-PASS ${passed[1]}`);
