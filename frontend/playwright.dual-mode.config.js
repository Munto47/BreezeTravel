const { defineConfig } = require('@playwright/test')

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: [
    'dual-mode-boundaries.spec.js',
    'local-controlled.spec.js',
    'progressive-result.spec.js',
  ],
  timeout: 45_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:3110',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list']],
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3110',
    url: 'http://127.0.0.1:3110',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:8999',
      NEXT_PUBLIC_Y_WEBSOCKET_URL: 'ws://127.0.0.1:8998',
    },
  },
})
