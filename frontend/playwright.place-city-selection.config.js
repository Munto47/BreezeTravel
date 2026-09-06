const { defineConfig } = require('@playwright/test')
const path = require('node:path')

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'place-city-selection.spec.js',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3141',
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list']],
  outputDir: path.resolve(__dirname, '../.local-artifacts/place-city-browser'),
  webServer: process.env.E2E_BASE_URL ? undefined : {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3141',
    url: 'http://127.0.0.1:3141',
    reuseExistingServer: false,
    timeout: 120_000,
    env: { BACKEND_INTERNAL_URL: 'http://127.0.0.1:8999' },
  },
})
