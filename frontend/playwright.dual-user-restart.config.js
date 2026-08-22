const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'dual-user-restart-matrix.spec.js',
  timeout: 600_000,
  globalTimeout: 720_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:3104',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  reporter: [
    ['list'],
    ['json', { outputFile: '../backend/evidence/full_stack/dual_user_backend_yjs_restart_playwright.json' }],
  ],
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3104',
    url: 'http://127.0.0.1:3104',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:8000',
      NEXT_PUBLIC_Y_WEBSOCKET_URL: 'ws://127.0.0.1:1234',
    },
  },
});
