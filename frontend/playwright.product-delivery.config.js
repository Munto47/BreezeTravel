const { defineConfig } = require('@playwright/test')


module.exports = defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:3101',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list'], ['html', { outputFolder: 'playwright-report/product-delivery', open: 'never' }]],
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3101',
    url: 'http://127.0.0.1:3101',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL:
        process.env.PRODUCT_DELIVERY_API_URL || 'http://127.0.0.1:8999',
      NEXT_PUBLIC_SHOW_TEST_LOGIN: 'true',
    },
  },
})
