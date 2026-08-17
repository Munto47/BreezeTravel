const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'chat-persistence.spec.js',
  timeout: 45_000,
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:3102',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list']],
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3102',
    url: 'http://127.0.0.1:3102',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:8000',
      NEXT_PUBLIC_Y_WEBSOCKET_URL: 'ws://127.0.0.1:1234',
    },
  },
});
