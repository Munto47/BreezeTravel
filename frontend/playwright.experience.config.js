const { defineConfig } = require('@playwright/test')

module.exports = defineConfig({
  testDir: './e2e',
  testMatch: ['share-complete-export.spec.js', 'collaboration-relative-local.spec.js', 'relative-route-suggestions.spec.js', 'source-meal-selection.spec.js', 'four-pages-home.spec.js', 'named-meal-area.spec.js', 'edit-history.spec.js', 'daily-dining-expansion.spec.js', 'four-pages-cards.spec.js', 'four-pages-progress.spec.js', 'task-library-recovery.spec.js', 'relative-only-presentation.spec.js', 'confirm-source-details.spec.js', 'experience.spec.js', 'account-save-recovery.spec.js', 'semantic-plan-contract.spec.js', 'meal-export.spec.js', 'meal-preferences.spec.js', 'anonymous-meal-context.spec.js', 'alternative-export.spec.js', 'choice-selection.spec.js', 'experience-refinement.spec.js', 'confirmed-place-cards.spec.js', 'source-lodging.spec.js', 'pending-lodging-recovery.spec.js', 'map-day-focus.spec.js', 'stay-refresh-readback.spec.js', 'trip-capacity.spec.js', 'collaboration-backup.spec.js', 'g03r-result-ui.spec.js'],
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
