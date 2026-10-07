const {defineConfig} = require('@playwright/test')
module.exports = defineConfig({
  testDir: './e2e', testMatch: 'mobile-browser.spec.js', workers: 1, retries: 0,
  timeout: 45000, expect: {timeout: 10000},
  use: {baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3106', viewport: {width:390,height:844},
    trace:'retain-on-failure', screenshot:'only-on-failure'},
  outputDir:'test-results/mobile-browser', reporter:[['list']],
  projects:[
    {name:'mobile-chromium',grepInvert:/desktop layout/,use:{browserName:'chromium',isMobile:true,hasTouch:true}},
    {name:'mobile-webkit',grepInvert:/desktop layout/,use:{browserName:'webkit',isMobile:true,hasTouch:true}},
    {name:'desktop-chromium',grep:/desktop layout/,use:{browserName:'chromium'}},
  ],
})
