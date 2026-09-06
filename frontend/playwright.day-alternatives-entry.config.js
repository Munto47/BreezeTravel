const { defineConfig } = require('@playwright/test')
const path = require('node:path')

const artifactName = `day-alternatives-entry-${Date.now()}`

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'day-alternatives-entry.spec.js',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3137',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  // The main task owns the production build and already-running server.
  outputDir: path.resolve(__dirname, '../.local-artifacts', artifactName),
  reporter: [['list'], ['json', { outputFile: path.resolve(__dirname, '../.local-artifacts', `${artifactName}-results.json`) }]],
})
