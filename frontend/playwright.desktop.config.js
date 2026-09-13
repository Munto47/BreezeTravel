const { defineConfig } = require('@playwright/test')
const experience = require('./playwright.experience.config')
const { suites } = require('../docs/testing/product_test_manifest.json')

// Current delivery scope is computer browsers. Historical small-screen cases
// remain in the experience suite; this entry never schedules them.
module.exports = defineConfig(experience, {
  testMatch: suites['browser-four-pages'].files.map(file => file.replace(/^e2e\//, '')),
  grepInvert: /@small-screen|@live/,
  use: { viewport: { width: 1440, height: 1000 } },
  reporter: [
    ['list'],
    ['json', { outputFile: 'test-results/desktop-report.json' }],
  ],
})
