const { defineConfig } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');


const commitSha = execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: path.resolve(__dirname, '..'),
  encoding: 'utf8',
}).trim();
const reportPath = process.env.P6_G5_PLAYWRIGHT_JSON;

if (!reportPath || !path.isAbsolute(reportPath)) {
  throw new Error('P6_G5_PLAYWRIGHT_JSON_ABSOLUTE_REQUIRED');
}


module.exports = defineConfig({
  testDir: './e2e',
  testMatch: ['trip-check-p1.spec.js', 'trip-check-p3.spec.js'],
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  metadata: {
    commit_sha: commitSha,
    evidence_class: 'CONTROLLED_BROWSER_FIXTURE',
    evidence_scope: 'P6_G5_LOCAL_CHAIN',
    live_provider_evidence: false,
    public_e2e_evidence: false,
    human_evidence: false,
  },
  use: {
    baseURL: 'http://127.0.0.1:3106',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [
    ['list'],
    ['json', { outputFile: reportPath }],
  ],
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3106',
    url: 'http://127.0.0.1:3106',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:8999',
      NEXT_PUBLIC_Y_WEBSOCKET_URL: 'ws://127.0.0.1:8998',
      NEXT_PUBLIC_TRIP_CHECK_COMMIT_SHA: commitSha,
    },
  },
});
