const {defineConfig}=require('@playwright/test')
const label=process.env.RELEASE_TEST_LABEL||'local'
module.exports=defineConfig({testDir:'./e2e',testMatch:'first-release-live.spec.js',timeout:240000,workers:1,retries:0,
 use:{baseURL:process.env.E2E_BASE_URL||'http://127.0.0.1:3118',launchOptions:process.env.FIRST_RELEASE_PRIVATE_TUNNEL==='1'?{args:['--host-resolver-rules=MAP www.breezetravel.cn 127.0.0.1','--no-proxy-server']}:undefined,viewport:{width:1440,height:900},actionTimeout:15000,trace:'off',screenshot:'only-on-failure'},
 outputDir:`test-results-first-release-${label}`,reporter:[['list'],['json',{outputFile:`../.local-artifacts/first-release-${label}-report.json`}]]})
