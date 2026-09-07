const {defineConfig}=require('@playwright/test')
module.exports=defineConfig({
  testDir:'./e2e', testMatch:['g03r-result-ui.spec.js','progressive-result.spec.js','dual-mode-boundaries.spec.js'],
  timeout:45000, workers:1, retries:0,
  use:{baseURL:'http://127.0.0.1:3117',trace:'retain-on-failure',screenshot:'only-on-failure'},
  reporter:[['list']],
  webServer:{command:'npm run dev -- --hostname 127.0.0.1 --port 3117',url:'http://127.0.0.1:3117',reuseExistingServer:false,timeout:120000,
    env:{NEXT_PUBLIC_API_URL:'http://127.0.0.1:8999',NEXT_PUBLIC_Y_WEBSOCKET_URL:'ws://127.0.0.1:8998'}}
})
