const { defineConfig } = require('@playwright/test')
const productDelivery = require('./playwright.product-delivery.config')

module.exports = defineConfig({
  ...productDelivery,
  testMatch: [
    'trip-understanding-v3.spec.js',
    'g02-product-delivery.spec.js',
    'g03-product-delivery.spec.js',
    'g03r-result-ui.spec.js',
    'g04-screenshot-parity.spec.js',
    'g05-knowledge.spec.js',
    'g06-memory-share.spec.js',
  ],
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report/candidate', open: 'never' }],
  ],
})
