const {defineConfig}=require('@playwright/test')
module.exports=defineConfig({
 testDir:'./e2e',testMatch:'live-generation.spec.js',workers:1,
 outputDir:'../.local-artifacts/live-card-workspace/browser-tests',
 use:{baseURL:'http://127.0.0.1:3185',viewport:{width:1440,height:1000}},
 webServer:[
  {command:'node e2e/support/live-generation-server.cjs',port:8185,env:{PORT:'8185'}},
  {command:'npm run dev -- --hostname 127.0.0.1 --port 3185',url:'http://127.0.0.1:3185',env:{BACKEND_INTERNAL_URL:'http://127.0.0.1:8185'},timeout:120000},
 ],
})
