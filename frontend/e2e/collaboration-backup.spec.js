const {test, expect} = require('@playwright/test')

function place(id, name, category = 'attraction') {
  return {placeId: id, name, category, address: '测试地址', city: '北京',
    coords: {lng: 116.39, lat: 39.92}, source: 'amap_poi', tags: [], amapPhotos: []}
}
const unscheduled = place('selected-too-long', '颐和园')
function savedRoute(withBackup = true) {
  return {itineraryId: 'saved-route', threadId: 'backup-thread', city: '北京',
    generatedAt: '2026-09-08T06:00:00Z', version: 1,
    days: [{dayIndex: 0, clusterId: 0, slots: [{placeId: 'scheduled',
      place: place('scheduled', '胡大饭馆(簋街三店)', 'food'),
      startTime: '18:00', endTime: '19:00', tips: []}]}],
    ...(withBackup ? {backupPool: [unscheduled]} : {})}
}

async function setup(page, routeData) {
  let writes = 0
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'controlled-room-token')
    localStorage.setItem('authUser', JSON.stringify({userId: 'room-owner', nickname: '测试同行者'}))
  })
  await page.route('**/webapi.amap.com/**', route => route.abort())
  await page.route('**/api/**', route => {
    const pathname = new URL(route.request().url()).pathname
    const reply = json => route.fulfill({json})
    if (pathname === '/api/user/me') return reply({user_id: 'room-owner', nickname: '测试同行者'})
    if (pathname.endsWith('/state')) return reply({thread_id: 'backup-thread', trip_city: '北京', trip_days: 1})
    if (pathname.endsWith('/places')) return reply([])
    if (pathname.endsWith('/current-itinerary')) return reply({room_id:'BACKUP42',version:0,
      itinerary_data:null,selection_snapshot:null,published_at:null})
    if (pathname.endsWith('/itinerary')) {
      if (route.request().method() === 'POST') writes++
      return reply({itinerary_data: routeData})
    }
    if (pathname.endsWith('/ws-token')) return route.fulfill({status: 503, json: {}})
    if (pathname === '/api/weather') return reply({city: '北京', days: []})
    return route.fulfill({status: 404, json: {}})
  })
  return () => writes
}

for (const width of [1440, 390]) test(`saved unscheduled selection remains visible after refresh at ${width}px`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}, info) => {
  await page.setViewportSize({width, height: 900})
  const writes = await setup(page, savedRoute())
  await page.goto('/room/BACKUP42')
  const drawer = page.getByRole('region', {name: '尚未排入的地点'})
  await expect(drawer).toContainText('颐和园')
  await expect(drawer).toContainText('尚未排入每日路线')
  // A preserved selection must not offer an action that toggles it off.
  await expect(drawer.getByRole('button', {name: '加入行程'})).toHaveCount(0)
  await page.getByLabel('关闭未排入地点').click()
  await expect(page.getByTestId('collaboration-unassigned')).toContainText('1 个已选地点')
  await page.reload()
  await expect(drawer).toContainText('颐和园')
  await page.getByLabel('关闭未排入地点').click()
  await page.getByTestId('collaboration-unassigned').click()
  await expect(drawer).toContainText('颐和园')
  expect(writes()).toBe(0)
  await page.screenshot({path: info.outputPath('retained-unscheduled-selection.png')})
})

test('old saved route without a backup field remains readable', async ({page}) => {
  await page.setViewportSize({width:1440,height:900})
  const writes = await setup(page, savedRoute(false))
  await page.goto('/room/BACKUP42')
  await expect(page.getByRole('button', {name: '转入行程查', exact: true})).toBeEnabled()
  await expect(page.getByTestId('collaboration-unassigned')).toHaveCount(0)
  // The old clock fields remain readable data, but September scope displays relative order.
  const route = page.locator('[data-testid="collaboration-relative-itinerary"]:visible')
  await expect(route).toContainText('第 1 站')
  await expect(route).not.toContainText(/18:00|19:00/)
  expect(writes()).toBe(0)
})
