const { defineConfig } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');


const commitSha = execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: path.resolve(__dirname, '..'),
  encoding: 'utf8',
}).trim();


module.exports = defineConfig({
  testDir: './e2e',
  testMatch: ['trip-check-p1.spec.js'],
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  metadata: {
    commit_sha: commitSha,
    evidence_class: 'CONTROLLED_BROWSER_FIXTURE',
    reliability_cases: ['refresh_resume', 'sse_reconnect', 'duplicate_event_deduplication'],
    live_provider_evidence: false,
    public_e2e_evidence: false,
    human_evidence: false,
  },
  use: {
    baseURL: 'http://127.0.0.1:3102',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [
    ['list'],
    ['json', { outputFile: '../backend/evidence/trip_check_v1/p2/browser-playwright.json' }],
  ],
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3102',
    url: 'http://127.0.0.1:3102',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:8999',
      NEXT_PUBLIC_Y_WEBSOCKET_URL: 'ws://127.0.0.1:8998',
      NEXT_PUBLIC_TRIP_CHECK_COMMIT_SHA: commitSha,
    },
  },
});
