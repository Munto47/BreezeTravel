const {test, expect} = require('@playwright/test')
const {openDemo, readResult} = require('./support/current-experience')

const ORIGINAL = [
  ['故宫博物院', '景山公园'],
  ['天坛公园', '前门大街'],
  ['颐和园', '圆明园'],
]
const EDIT_ONE = [[...ORIGINAL[0]].reverse(), ORIGINAL[1], ORIGINAL[2]]
const EDIT_TWO = [EDIT_ONE[0], [...ORIGINAL[1]].reverse(), ORIGINAL[2]]
const NEW_BRANCH = [EDIT_ONE[0], ORIGINAL[1], [...ORIGINAL[2]].reverse()]

async function assertStored(page, names, canUndo, canRedo, {changed = true} = {}) {
  await expect.poll(async () => {
    const {body} = await readResult(page)
    return {
      names: body.days.map(day => day.activities.map(card => card.name)),
      canUndo: body.can_undo,
      canRedo: body.can_redo,
    }
  }).toEqual({names, canUndo, canRedo})
  const stored = await readResult(page)
  expect(stored.body.is_demo).toBe(true)
  expect(stored.body.ownership).toBe('ANONYMOUS')
  expect(stored.body.coverage.confirmed_place_count).toBe(6)
  expect(stored.body.coverage.unresolved_place_count).toBe(0)
  expect(stored.body.days.flatMap(day => day.activities).every(card => card.status === 'READY')).toBe(true)
  expect(JSON.stringify(stored.body)).not.toContain('edit_history')
  if (changed) expect(stored.body.map.status).toBe('NEEDS_UPDATE')
  await expect(page.getByTestId('drag-handle-1-0')).toBeEnabled()
  await expect(page.getByTestId('activity-card').getByRole('heading')).toHaveText(names.flat())
  await expect(page.getByTestId('undo-trip-command')).toBeEnabled({enabled: canUndo})
  await expect(page.getByTestId('redo-trip-command')).toBeEnabled({enabled: canRedo})
  return stored
}

async function moveFirstOfDay(page, day) {
  const handle = page.getByTestId(`drag-handle-${day}-0`)
  await expect(handle).toBeEnabled()
  await handle.press('Enter')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('Enter')
}

async function applyAndCheck(page, action, commandType, names, canUndo, canRedo, etags) {
  const before = await readResult(page)
  const responsePromise = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/commands'))
  await action()
  const response = await responsePromise
  expect(response.status()).toBe(200)
  expect(response.request().postDataJSON().command_type).toBe(commandType)
  expect(response.request().headers()['if-match']).toBe(before.etag)
  const stored = await assertStored(page, names, canUndo, canRedo)
  expect(stored.etag).not.toBe(before.etag)
  expect(etags).not.toContain(stored.etag)
  etags.push(stored.etag)
  return stored
}

for (const width of [1440, 390]) {
  test(`real saved undo and redo history survives reload and new branch at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 900})
    await openDemo(page)
    const initial = await assertStored(page, ORIGINAL, false, false, {changed: false})
    const etags = [initial.etag]
    const writes = []
    page.on('request', request => {
      const pathname = new URL(request.url()).pathname
      if (pathname.startsWith('/api/v3/trip-understandings/') && request.method() === 'POST') {
        writes.push({action: pathname.split('/').pop(), command: request.postDataJSON()?.command_type})
      }
    })
    await applyAndCheck(page, () => moveFirstOfDay(page, 1), 'ACTIVITY_MOVE', EDIT_ONE, true, false, etags)
    await applyAndCheck(page, () => moveFirstOfDay(page, 2), 'ACTIVITY_MOVE', EDIT_TWO, true, false, etags)
    await page.reload()
    expect((await assertStored(page, EDIT_TWO, true, false)).etag).toBe(etags.at(-1))
    await applyAndCheck(page, () => page.getByTestId('undo-trip-command').click(), 'UNDO', EDIT_ONE, true, true, etags)
    await applyAndCheck(page, () => page.getByTestId('undo-trip-command').click(), 'UNDO', ORIGINAL, false, true, etags)
    await page.reload()
    expect((await assertStored(page, ORIGINAL, false, true)).etag).toBe(etags.at(-1))
    await page.screenshot({path: testInfo.outputPath(`two-undos-${width}.png`), fullPage: true})
    await applyAndCheck(page, () => page.getByTestId('redo-trip-command').click(), 'REDO', EDIT_ONE, true, true, etags)
    await applyAndCheck(page, () => page.getByTestId('redo-trip-command').click(), 'REDO', EDIT_TWO, true, false, etags)
    await page.screenshot({path: testInfo.outputPath(`two-redos-${width}.png`), fullPage: true})
    await applyAndCheck(page, () => page.getByTestId('undo-trip-command').click(), 'UNDO', EDIT_ONE, true, true, etags)
    await applyAndCheck(page, () => moveFirstOfDay(page, 3), 'ACTIVITY_MOVE', NEW_BRANCH, true, false, etags)
    await page.reload()
    expect((await assertStored(page, NEW_BRANCH, true, false)).etag).toBe(etags.at(-1))
    const commands = writes.filter(write => write.action === 'commands')
    expect(commands).toEqual([
      'ACTIVITY_MOVE', 'ACTIVITY_MOVE', 'UNDO', 'UNDO', 'REDO', 'REDO', 'UNDO', 'ACTIVITY_MOVE',
    ].map(command => ({action: 'commands', command})))
    // The current page refreshes its derived checking view after edits/reload.
    // It must not adopt suggestions, request places or update routes implicitly.
    expect(writes.filter(write => write.action !== 'commands').every(write => write.action === 'materialize')).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({path: testInfo.outputPath(`new-branch-${width}.png`), fullPage: true})
    await testInfo.attach('saved-history-checks', {contentType: 'application/json', body: JSON.stringify({
      viewport: width, commandCount: commands.length, checkViewRequests: writes.length - commands.length,
      uniqueSavedVersions: etags.length,
      finalNames: NEW_BRANCH, canUndo: true, canRedo: false, map: 'NEEDS_UPDATE',
      externalModelAndPlaceCalls: 0, fixture: 'Real DEMO API / fixed server providers; real anonymous cookie and PostgreSQL',
    })})
  })
}
