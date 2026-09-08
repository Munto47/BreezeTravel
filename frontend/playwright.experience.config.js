const { defineConfig } = require('@playwright/test')

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: ['experience.spec.js', 'account-save-recovery.spec.js', 'semantic-plan-contract.spec.js', 'experience-refinement.spec.js', 'confirmed-place-cards.spec.js', 'source-lodging.spec.js', 'pending-lodging-recovery.spec.js', 'map-day-focus.spec.js', 'stay-refresh-readback.spec.js', 'trip-capacity.spec.js', 'collaboration-backup.spec.js', 'g03r-result-ui.spec.js'],
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
