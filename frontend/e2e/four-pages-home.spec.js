const {test, expect} = require('@playwright/test')

// Home interaction checks only: every API call is intercepted. No model, POI,
// account or business database writes are made by these fixtures.
async function fixture(page, {accepted = false} = {}) {
  const creates = []
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v3/trip-understandings' && request.method() === 'POST') {
      creates.push({body: request.postDataJSON(), key: request.headers()['idempotency-key']})
      await new Promise(resolve => setTimeout(resolve, 200))
      return route.fulfill({status: accepted ? 202 : 503, contentType: 'application/json', body: JSON.stringify(accepted
        ? {public_resource_id: 'home-controlled-reference', status: 'PROCESSING', message: '正在整理', result_url: '', events_url: ''}
        : {detail: 'Controlled home-only failure'})})
    }
    if (path.endsWith('/result')) return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({status:'PROCESSING', message:'正在整理', phase:'RECEIVED', retry_after_ms:10000, event_cursor:0, progress:{day_count:0,card_count:0,places_checked:0,places_total:0},snapshot:null})})
    if (path.endsWith('/events')) return route.fulfill({status:200, contentType:'text/event-stream',body:''})
    return route.fulfill({status:404, contentType:'application/json', body:'{}'})
  })
  await page.goto('/')
  await expect(page.getByTestId('trip-source-text')).toBeEnabled()
  await expect(page.getByTestId('create-full-trip')).toBeDisabled()
  return creates
}

for (const width of [1440, 390]) test(`four-page home: real text entry, labelled preview and guide fit ${width}px`, { tag: width === 390 ? '@small-screen' : '@desktop' }, async ({page}, info) => {
  await page.setViewportSize({width, height: width === 1440 ? 1024 : 844})
  const creates = await fixture(page)
  await expect(page.getByRole('heading',{level:1})).toHaveText('把旅行想法，变成清晰的行程')
  await expect(page.getByTestId('source-character-count')).toHaveText('0 / 50,000')
  await expect(page.getByTestId('trip-source-text')).toHaveAttribute('maxlength','50000')
  await expect(page.getByRole('button',{name:'粘贴内容',exact:true})).toBeVisible()
  await expect(page.getByLabel('填入文字示例').getByRole('button')).toHaveCount(4)
  await expect(page.locator('input[type=file]')).toHaveCount(0)
  await expect(page.getByText('示意地图 · 非实际路线')).toBeVisible()
  await expect(page.getByTestId('home-example-preview')).toContainText('静态展示示例，不是本次整理结果')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  await page.screenshot({path:info.outputPath(`home-${width}.png`),fullPage:true})
  await page.getByRole('link',{name:'使用指南',exact:true}).click()
  await expect(page.getByRole('heading',{level:1})).toHaveText('让旅行安排，清楚一点。')
  await expect(page.locator('.four-guide-steps > li')).toHaveCount(5)
  await expect(page.getByRole('link',{name:'行程查',exact:true})).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  await page.screenshot({path:info.outputPath(`guide-${width}.png`),fullPage:true})
  expect(creates).toHaveLength(0)
})

for (const id of ['start-demo','home-example-shanghai','home-example-family','home-example-food']) test(`example ${id} fills text then submits FULL once with same-key failure recovery`, async ({page}) => {
  const creates = await fixture(page)
  await page.getByTestId(id).click()
  const text = await page.getByTestId('trip-source-text').inputValue()
  expect(text.length).toBeGreaterThan(30)
  expect(text).not.toMatch(/https?:|\d{1,2}:\d{2}/)
  expect(creates).toHaveLength(0)
  await page.getByTestId('create-full-trip').dblclick()
  await expect(page.locator('#home-input-error')).toContainText('文字仍在这里')
  expect(creates).toHaveLength(1)
  expect(creates[0].body).toEqual({mode:'FULL', source:{type:'TEXT',text}})
  await page.reload()
  await expect(page.getByTestId('trip-source-text')).toHaveValue(text)
  await page.getByTestId('create-full-trip').click()
  await expect(page.locator('#home-input-error')).toBeVisible()
  expect(creates).toHaveLength(2)
  expect(creates[1].key).toBe(creates[0].key)
})

test('sample and clipboard replacements preserve the draft until confirmed; keyboard can cancel', async ({page}) => {
  await page.addInitScript(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{readText:async()=> '剪贴板攻略：杭州第1天，先到断桥，再去孤山。'}}))
  const creates = await fixture(page)
  await page.getByTestId('trip-source-text').fill('我的原有攻略，先去故宫，再去景山。')
  await page.getByTestId('start-demo').click()
  await expect(page.getByRole('dialog',{name:'替换当前文字？'})).toBeVisible()
  await expect(page.getByRole('button',{name:'保留我的文字',exact:true})).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('start-demo')).toBeFocused()
  await expect(page.getByTestId('trip-source-text')).toHaveValue('我的原有攻略，先去故宫，再去景山。')
  await page.getByRole('button',{name:'粘贴内容',exact:true}).click()
  await page.getByRole('button',{name:'替换为粘贴文字',exact:true}).click()
  await expect(page.getByTestId('trip-source-text')).toHaveValue('剪贴板攻略：杭州第1天，先到断桥，再去孤山。')
  await page.reload()
  await expect(page.getByTestId('trip-source-text')).toHaveValue('剪贴板攻略：杭州第1天，先到断桥，再去孤山。')
  expect(creates).toHaveLength(0)
})

test('clipboard denial or over-limit content preserves the typed source without submission', async ({page}) => {
  await page.addInitScript(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{readText:async()=>{throw new Error('denied')}}}))
  const creates = await fixture(page)
  await page.getByTestId('trip-source-text').fill('原有文字')
  await page.getByRole('button',{name:'粘贴内容',exact:true}).click()
  await expect(page.getByRole('status')).toContainText('未能读取剪贴板')
  await expect(page.getByTestId('trip-source-text')).toHaveValue('原有文字')
  await page.evaluate(() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{readText:async()=> '字'.repeat(50001)}}))
  await page.getByRole('button',{name:'粘贴内容',exact:true}).click()
  await expect(page.getByRole('status')).toContainText('超过 50,000 字')
  await expect(page.getByTestId('trip-source-text')).toHaveValue('原有文字')
  expect(creates).toHaveLength(0)
})

test('successful anonymous TEXT creation remembers only its public reference', async ({page}) => {
  const creates = await fixture(page,{accepted:true})
  await page.getByTestId('home-example-shanghai').click()
  await page.getByTestId('create-full-trip').click()
  await expect(page).toHaveURL(/\/trip\/result#trip=home-controlled-reference/)
  expect(creates).toHaveLength(1)
  const local = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)))
  const matching = Object.entries(local).filter(([,value]) => value.includes('home-controlled-reference'))
  expect(matching).toHaveLength(1)
  expect(JSON.parse(matching[0][1])).toEqual([{resource:'home-controlled-reference'}])
  expect(Object.values(local).some(value => value.includes(creates[0].body.source.text))).toBe(false)
})
