const { defineConfig } = require('@playwright/test')
const path = require('node:path')

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'journey-suggestions.spec.js',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3137',
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  outputDir: path.resolve(__dirname, '../.local-artifacts/quality-browser'),
  reporter: [['list'], ['json', { outputFile: path.resolve(__dirname, '../.local-artifacts/quality-browser-results.json') }]],
})
