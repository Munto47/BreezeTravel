const {defineConfig}=require('@playwright/test')
module.exports=defineConfig({testDir:'./e2e',testMatch:'soft-canvas-live.spec.js',timeout:150000,workers:1,retries:0,
 use:{baseURL:'http://127.0.0.1:3118',viewport:{width:1440,height:900},trace:'retain-on-failure'},
 outputDir:'test-results-live-soft',reporter:[['list'],['json',{outputFile:'../.local-artifacts/soft-canvas-live-report.json'}]]})
