const { defineConfig } = require('@playwright/test')
const path = require('node:path')

// A separately named run preserves all earlier real-service evidence.
const artifactName = process.env.E2E_QUALITY_LIVE_ARTIFACT_NAME || `quality-live-browser-${Date.now()}`
if (!/^[a-z0-9][a-z0-9_-]{0,79}$/i.test(artifactName)) throw new Error('Invalid local artifact name')
process.env.E2E_QUALITY_LIVE_ARTIFACT_NAME = artifactName

// Uses an already running, owner-authorized local production build and real API.
// Deliberately no webServer, retries, storageState, HAR, video or network trace.
module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'itinerary-quality-live.spec.js',
  timeout: 240_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3137',
    viewport: { width: 1440, height: 900 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    acceptDownloads: true,
    trace: 'off',
    video: 'off',
    screenshot: 'only-on-failure',
  },
  outputDir: path.resolve(__dirname, '../.local-artifacts', artifactName),
  reporter: [['list'], ['json', {
    outputFile: path.resolve(__dirname, '../.local-artifacts', `${artifactName}-results.json`),
  }]],
})
