const {test,expect}=require('@playwright/test')

// Owner-authorized existing local providers. Synthetic text only; no personal itinerary.
test('live soft canvas: real text, event progress, POI photo availability and map overview',async({page},testInfo)=>{
 let eventConnections=0
 const phases=new Set()
 page.on('request',request=>{if(new URL(request.url()).pathname.endsWith('/events')) eventConnections++})
 await page.goto('/')
 await page.getByTestId('trip-source-text').fill('北京两日游。Day 1 上午9点去故宫博物院，下午1点去景山公园。Day 2 上午10点去天坛公园。')
 await page.getByTestId('create-full-trip').click()
 await expect(page.getByTestId('generation-stages')).toBeVisible()
 await page.screenshot({path:testInfo.outputPath('generation.png'),fullPage:true})
 // Observe real UI state while the server runs; no synthetic phase advancement.
 await page.evaluate(()=>{
   window.__generationStates=[]
   const observe=()=>{const step=document.querySelector('[aria-current="step"]'); if(step) window.__generationStates.push(step.textContent)}
   window.__generationObserver=new MutationObserver(observe)
   window.__generationObserver.observe(document.body,{subtree:true,childList:true,attributes:true})
   observe()
 })
 await expect(page.getByTestId('itinerary-workspace')).toBeVisible({timeout:120000})
 const observations=await page.evaluate(()=>{window.__generationObserver.disconnect();return window.__generationStates})
 observations.forEach(value=>phases.add(value))
 const cards=await page.getByTestId('activity-card').count()
 expect(cards).toBeGreaterThan(0)
 expect(eventConnections).toBeGreaterThan(0)
 await expect(page.getByTestId('drag-handle-1-0')).toBeEnabled()
 await page.screenshot({path:testInfo.outputPath('cards.png'),fullPage:true})
 const photos=await page.getByTestId('activity-card').locator('img').evaluateAll(nodes=>nodes.map(n=>({loaded:n.complete&&n.naturalWidth>0})))
 await page.getByTestId('desktop-nav-map_stay').click()
 await expect(page.getByRole('heading',{name:'全程地图'})).toBeVisible()
 await expect(page.getByTestId('map-place-directory')).toContainText('故宫')
 await expect(page.getByTestId('map-place-directory')).toContainText('天坛')
 await expect(page.locator('.e-map-marker')).toHaveCount(3,{timeout:45000})
 await page.getByTestId('route-map').scrollIntoViewIfNeeded()
 for (const marker of await page.locator('.e-map-marker').all()) await expect(marker).toBeInViewport({timeout:30000})
 await page.mouse.move(1300,100)
 await page.getByRole('heading',{name:'全程地图'}).click()
 await page.screenshot({path:testInfo.outputPath('map.png'),fullPage:true})
 await testInfo.attach('live-observations',{body:JSON.stringify({cards,eventConnections,visibleStages:[...phases],photos},null,2),contentType:'application/json'})
})
