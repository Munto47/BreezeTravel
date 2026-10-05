const {test, expect} = require('@playwright/test')
const {openDemo, readResult, moveFirst} = require('./support/current-experience')

test.use({viewport:{width:1440,height:1000},actionTimeout:15000})

async function editPlace(page, value) {
  await page.getByLabel('故宫博物院更多操作',{exact:true}).click()
  await page.getByRole('button',{name:'查看详情',exact:true}).click()
  await page.getByLabel('搜索地点名称',{exact:true}).fill(value)
  await page.getByLabel('查询城市',{exact:true}).selectOption('上海')
}

for(let round=0;round<Number(process.env.TRIPCHECK_RELIABILITY_ROUNDS || 1);round++) {
  test(`two editors preserve search input after a real version conflict ${round}`,async({page,context})=>{
    await openDemo(page)
    const peer=await context.newPage()
    await peer.goto(page.url())
    await expect(peer.getByTestId('itinerary-workspace')).toBeVisible()
    // Candidate discovery is controlled; the stale command uses the real API
    // and must fail before any candidate can alter the saved itinerary.
    await page.route('**/place-candidates',route=>route.fulfill({json:{status:'AVAILABLE',candidates:[{
      candidate_token:'c'.repeat(43),name:'青溪公园',category:'景点',area_or_address:'合成查询地址',
      position:{longitude:121.4,latitude:31.2,coordinate_system:'GCJ02'},
    }]}}))
    await editPlace(page,'甲窗口的未保存输入')
    await page.getByRole('button',{name:'搜索',exact:true}).click()
    await page.getByRole('button',{name:/青溪公园.*合成查询地址/}).click()
    await moveFirst(peer)
    await expect(peer.getByTestId('activity-card').first().getByRole('heading')).toHaveText('景山公园')
    const conflict=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname.endsWith('/commands'))
    await page.getByRole('button',{name:'使用这个地点',exact:true}).click()
    expect((await conflict).status()).toBe(409)
    await expect(page.getByTestId('activity-card').first().getByRole('heading')).toHaveText('景山公园')
    await page.getByLabel('故宫博物院更多操作',{exact:true}).click()
    await page.getByRole('button',{name:'查看详情',exact:true}).click()
    await expect(page.getByLabel('搜索地点名称',{exact:true})).toHaveValue('甲窗口的未保存输入')
    await expect(page.getByLabel('查询城市',{exact:true})).toHaveValue('上海')
    expect((await readResult(peer)).body.days[0].activities.map(a=>a.name)).toEqual(['景山公园','故宫博物院'])
  })

  test(`late result response cannot replace a different opened trip ${round}`,async({page})=>{
    const original=await openDemo(page)
    await moveFirst(page)
    await expect(page.getByTestId('activity-card').first().getByRole('heading')).toHaveText('景山公园')
    await expect.poll(()=>page.evaluate(()=>sessionStorage.getItem('bt_pending_operation'))).toBeNull()
    let release
    const held=new Promise(resolve=>{release=resolve})
    let arrived=false
    await page.route(`**/trip-understandings/${original}/result`,async route=>{
      const response=await route.fetch()
      arrived=true
      await held
      await route.fulfill({response}).catch(()=>{})
    })
    await page.reload({waitUntil:'domcontentloaded'})
    await expect.poll(()=>arrived).toBe(true)
    const replacement=await openDemo(page)
    expect(replacement).not.toBe(original)
    const before=await readResult(page)
    release()
    await expect(page.getByTestId('activity-card').first().getByRole('heading')).toHaveText('故宫博物院')
    expect((await readResult(page)).etag).toBe(before.etag)
    expect(await page.evaluate(()=>sessionStorage.getItem('bt_active_trip_ref'))).toBe(replacement)
  })

  test(`PNG generation rejects an itinerary changed before encoding completes ${round}`,async({page})=>{
    await page.addInitScript(()=>{
      const original=HTMLCanvasElement.prototype.toBlob
      HTMLCanvasElement.prototype.toBlob=function(...args){
        window.releaseItineraryPng=()=>original.apply(this,args)
        window.itineraryPngWaiting=true
      }
    })
    await openDemo(page)
    await page.getByTestId('export-itinerary-png').click()
    await expect.poll(()=>page.evaluate(()=>window.itineraryPngWaiting===true)).toBe(true)
    await moveFirst(page)
    await expect(page.getByTestId('activity-card').first().getByRole('heading')).toHaveText('景山公园')
    await page.evaluate(()=>window.releaseItineraryPng())
    await expect(page.getByRole('alert').filter({hasText:'生成期间行程已经更新'})).toBeVisible()
    await expect(page.getByTestId('download-itinerary-png')).toHaveCount(0)
    expect((await readResult(page)).body.days[0].activities[0].name).toBe('景山公园')
  })
}
