const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'suggestion-set-workspace.spec.js',
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:3104',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list']],
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3104',
    url: 'http://127.0.0.1:3104',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      // The test explicitly fulfills every product boundary it exercises.
      // It is a controlled frontend fixture, never public/provider evidence.
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:8999',
      NEXT_PUBLIC_Y_WEBSOCKET_URL: 'ws://127.0.0.1:8998',
    },
  },
});
