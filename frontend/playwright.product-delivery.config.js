const { defineConfig } = require('@playwright/test')

const productDeliveryApiUrl =
  process.env.PRODUCT_DELIVERY_API_URL || 'http://127.0.0.1:8999'
const productDeliveryFrontendPort =
  process.env.PRODUCT_DELIVERY_FRONTEND_PORT || '3101'
const productDeliveryFrontendUrl =
  `http://127.0.0.1:${productDeliveryFrontendPort}`

module.exports = defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: productDeliveryFrontendUrl,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list'], ['html', { outputFolder: 'playwright-report/product-delivery', open: 'never' }]],
  webServer: {
    command: `npm run dev -- --hostname 127.0.0.1 --port ${productDeliveryFrontendPort}`,
    url: productDeliveryFrontendUrl,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      // Client requests and Next's same-origin rewrite must target the same
      // delivery backend.  The latter otherwise falls back to localhost:8000.
      NEXT_PUBLIC_API_URL: productDeliveryApiUrl,
      BACKEND_INTERNAL_URL: productDeliveryApiUrl,
      NEXT_PUBLIC_SHOW_TEST_LOGIN: 'true',
    },
  },
})
