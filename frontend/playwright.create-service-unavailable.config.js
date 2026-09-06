const { defineConfig } = require('@playwright/test')
const path = require('node:path')

const artifactName = `create-service-unavailable-${Date.now()}`

// The main task builds and starts the production frontend. All API responses in
// these tests are controlled synthetic fixtures; no model or map service runs.
module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'create-service-unavailable.spec.js',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3137',
    viewport: { width: 1280, height: 800 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  outputDir: path.resolve(__dirname, '../.local-artifacts', artifactName),
  reporter: [['list'], ['json', {
    outputFile: path.resolve(__dirname, '../.local-artifacts', `${artifactName}-results.json`),
  }]],
})
