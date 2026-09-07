const { defineConfig } = require('@playwright/test')
const path = require('node:path')
module.exports = defineConfig({
  testDir: './e2e', testMatch: 'confirmed-place-cards.spec.js',
  timeout: 45000, expect: { timeout: 10000 }, workers: 1, retries: 0,
  use: { baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3137', viewport: { width: 1440, height: 900 },
    screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  outputDir: path.resolve(__dirname, '../.local-artifacts/confirmed-cards-browser'), reporter: [['list']],
})
