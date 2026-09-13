const {defineConfig}=require('@playwright/test')
const path=require('node:path')
process.env.RUN_LODGING_LIVE='1'
const baseURL=process.env.E2E_BASE_URL||'http://127.0.0.1:3168'
if(!['127.0.0.1','localhost'].includes(new URL(baseURL).hostname))throw new Error('This check creates local test trips only')
const outputDir=process.env.LODGING_LIVE_OUTPUT||'../.local-artifacts/evaluation/lodging-cross-city-live-v1'
module.exports=defineConfig({testDir:'./e2e',testMatch:'lodging-cross-city-live.spec.js',timeout:360000,workers:1,retries:0,
 use:{baseURL,viewport:{width:1440,height:950},actionTimeout:15000,trace:'off',screenshot:'only-on-failure',
  storageState:process.env.LODGING_LIVE_RESUME_DIR?path.join(process.env.LODGING_LIVE_RESUME_DIR,'browser-state.json'):undefined},
 outputDir:path.join(outputDir,'browser'),reporter:[['list'],['json',{outputFile:path.join(outputDir,'playwright-report.json')}]]})
