const {defineConfig}=require('@playwright/test')
const path=require('node:path')
const fs=require('node:fs')
const baseURL=process.env.E2E_BASE_URL||'http://127.0.0.1:3168'
if(!['127.0.0.1','localhost'].includes(new URL(baseURL).hostname))throw new Error('Capacity stress checks create local trips only')
const label=process.env.CAPACITY_LIVE_LABEL||`capacity-live-${Date.now()}`
if(!/^[a-z0-9_-]+$/i.test(label))throw new Error('Invalid local artifact label')
process.env.RUN_CAPACITY_LIVE='1'
process.env.CAPACITY_LIVE_LABEL=label
let storageState
if(process.env.CAPACITY_LIVE_RESUME_LABEL){
 if(!/^[a-z0-9_-]+$/i.test(process.env.CAPACITY_LIVE_RESUME_LABEL))throw new Error('Invalid resume label')
 storageState=path.resolve(__dirname,'../.local-artifacts/evaluation',process.env.CAPACITY_LIVE_RESUME_LABEL,'browser-state.json')
 if(!fs.existsSync(storageState))throw new Error('Resume requires the saved test browser session')
}
module.exports=defineConfig({testDir:'./e2e',testMatch:'trip-capacity-live.spec.js',timeout:600000,workers:1,retries:0,
 use:{baseURL,storageState,viewport:{width:1440,height:900},actionTimeout:15000,trace:'off',screenshot:'only-on-failure'},
 outputDir:path.resolve(__dirname,'../.local-artifacts/evaluation',label,'browser'),
 reporter:[['list'],['json',{outputFile:path.resolve(__dirname,'../.local-artifacts/evaluation',label,'playwright.json')}]]})
