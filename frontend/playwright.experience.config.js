const { defineConfig } = require('@playwright/test')

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: 'experience.spec.js',
  timeout: 90000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3106',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [
    ['list'],
    ['json', { outputFile: 'test-results/experience-report.json' }],
  ],
})
