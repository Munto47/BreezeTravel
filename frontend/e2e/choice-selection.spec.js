const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

let snapshots
test.beforeAll(() => {
  snapshots = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', `
import asyncio,json
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import UserFacingTripResult,ChoiceClearCommand,UndoCommand,ActivityMoveCommand,AlternativeInsertCommand
from tests.test_choice_group_selection import build_choice_states,select_command,build_unsupported_choice_result
from tests.test_source_choice_labels import build_source_label_states
async def main():
    values=await build_choice_states()
    states={k:UserFacingTripResult.model_validate(v) for k,v in values.items()}
    edges=[]
    def transition(start,end,command,**kwargs):
        states[end]=apply_public_command(states[start],command,**kwargs).result
        edges.append(dict(start=start,end=end,command=command.model_dump(mode='json',exclude_none=True)))
    edges.append(dict(start='before',end='selected',command=select_command(states['before']).model_dump(mode='json')))
    token=states['selected'].days[0].choice_selections[0].activity_tokens[0]
    edges.append(dict(start='selected',end='confirmed',command=dict(command_type='PLACE_CONFIRM',activity_token=token,candidate_token='fixed-candidate-not-sent-to-service-00000000')))
    def clear(start,**kwargs):
        return ChoiceClearCommand(command_type='CHOICE_CLEAR',day_index=1,choice_group_token=states[start].days[0].choice_selections[0].choice_group_token,**kwargs)
    transition('confirmed','cleared',clear('confirmed'))
    transition('cleared','restored',UndoCommand(command_type='UNDO'),undo_result=states['confirmed'])
    transition('restored','cleared_again',clear('restored'))
    transition('cleared_again','other',select_command(states['cleared_again'],1))
    transition('other','undone_other',UndoCommand(command_type='UNDO'),undo_result=states['cleared_again'])
    selected_token=states['confirmed'].days[0].choice_selections[0].activity_tokens[0]
    transition('confirmed','modified',ActivityMoveCommand(command_type='ACTIVITY_MOVE',activity_token=selected_token,target_day_index=2,target_position=0))
    transition('modified','detached',clear('modified',preserve_activities=True))
    transition('detached','restored_modified',UndoCommand(command_type='UNDO'),undo_result=states['modified'])
    # Saved real Shanghai answer retains its nested/cross-day ambiguity. The
    # only manual insertion below uses the real command, never a corrected raw.
    states['unsupported']=(await build_unsupported_choice_result()).public_result
    day=states['unsupported'].days[2]
    assert all(a.choice_group_selectable is False for a in day.alternatives)
    item=day.alternatives[0]
    insert=AlternativeInsertCommand(command_type='ALTERNATIVE_INSERT',day_index=3,position=len(day.activities),
        alternative_token=item.activity_token)
    states['inserted']=apply_public_command(states['unsupported'],insert).result
    edges.append(dict(start='unsupported',end='inserted',command=insert.model_dump(mode='json',exclude_unset=True)))
    transition('inserted','undone_insert',UndoCommand(command_type='UNDO'),undo_result=states['unsupported'])
    labeled=await build_source_label_states()
    states.update({k:UserFacingTripResult.model_validate(v) for k,v in labeled['states'].items()})
    edges.extend(labeled['edges'])
    print(json.dumps(dict(states={k:v.model_dump(mode='json') for k,v in states.items()},edges=edges),ensure_ascii=False))
asyncio.run(main())
`], {cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'}}))
})

async function show(page, width, initial = 'before', old = false, dayIndex = 1) {
  const state = {key: initial, commands: [], result: structuredClone(snapshots.states[initial])}
  if (old) for (const day of state.result.days) for (const item of day.alternatives || []) delete item.insertion_position
  await page.setViewportSize({width, height: 1000})
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    const reply = json => route.fulfill({json, headers: {ETag: `"fixed-choice-${state.commands.length}"`}})
    if (url.pathname.endsWith('/commands')) {
      const body = request.postDataJSON()
      const edge = snapshots.edges.find(e => e.start === state.key && e.command.command_type === body.command_type)
      expect(edge, JSON.stringify({state: state.key, body})).toBeTruthy()
      const expected = {...edge.command}
      if (expected.preserve_activities === false) delete expected.preserve_activities
      if (body.command_type === 'CHOICE_SELECT' && body.position === undefined) {
        const members = snapshots.states[state.key].days[body.day_index - 1].alternatives.filter(item => item.choice_group_token === body.choice_group_token)
        expect(members.every(item => item.insertion_position === expected.position)).toBe(true)
        delete expected.position
      }
      expect(body).toEqual(expected)
      state.commands.push(body)
      await new Promise(resolve => setTimeout(resolve, 200))
      state.key = edge.end
      state.result = structuredClone(snapshots.states[edge.end])
      return reply({status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
    }
    if (url.pathname === '/api/user/me') return route.fulfill({status: 401, json: {}})
    if (url.pathname.endsWith('/result')) return reply(state.result)
    if (url.pathname.endsWith('/place-candidates')) return reply({status: 'AVAILABLE', candidates: [{
      candidate_token: 'fixed-candidate-not-sent-to-service-00000000', name: '良渚文化村', category: '景点', city: '杭州', area_or_address: '固定身份候选，未调用真实服务',
    }]})
    if (url.pathname.endsWith('/map-renders/latest')) return reply({...state.result.map, points: [], days: []})
    if (url.pathname.endsWith('/stay-suggestions')) return reply(state.result.stay)
    if (url.pathname.endsWith('/daily-dining')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/materialize')) return reply({status: 'READY', message: '已准备', calendar: '按日期', party_size: 2, checks_available: true})
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '请核对安排', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto('/trip/result#trip=fixed-choice-selection-0001')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await page.getByTestId(`day-alternatives-${dayIndex}`).click()
  await expect(page.getByTestId('itinerary-choice-group')).toContainText('二选一')
  return state
}

async function changed(page, state, key) {
  await expect.poll(() => state.key).toBe(key)
  await expect(page.getByTestId('activity-card')).toHaveCount(state.result.days.flatMap(d => d.activities).filter(c => c.status === 'READY').length)
  await expect(page.getByTestId('journey-suggestions-toggle')).toBeEnabled()
}

function visitContent(value) {
  // Real commands rotate edit tokens. Compare every public content field,
  // retaining group/branch identity, while excluding only those edit links.
  return JSON.parse(JSON.stringify(value, (key, item) =>
    ['activity_token', 'before_activity_token', 'after_activity_token'].includes(key) ? undefined : item))
}

test('original A B labels remain selectable through refresh clear and undo at 1440px', async ({page}, info) => {
  // The response comes from fixed semantic/identity providers through the real
  // pipeline and commands. No source label is substituted in this page fixture.
  const state = await show(page, 1440, 'labeled_before')
  const group = page.getByTestId('itinerary-choice-group')
  const day = page.getByTestId('day-lane-1')
  await expect(day).toContainText('这一天的地点仍是备选')
  await expect(group.getByRole('button', {name: '选择方案：方案A', exact: true})).toBeEnabled()
  await expect(group.getByRole('button', {name: '选择方案：方案B', exact: true})).toBeEnabled()
  await expect(group).not.toContainText('方案一')
  await expect(group).not.toContainText('方案二')
  await page.screenshot({path: info.outputPath('source-a-b-before.png'), fullPage: true})
  await group.getByRole('button', {name: '选择方案：方案A', exact: true}).click()
  await changed(page, state, 'labeled_selected')
  expect(state.result.days.map(d => d.activities.map(c => c.name))).toEqual([['良渚文化村'], ['西湖']])
  expect(state.result.days[0].activities[0].status).toBe('NEEDS_CONFIRMATION')
  await page.reload()
  await expect(day).toContainText('这一天已安排 1 个地点，待确认后显示卡片。')
  await expect(day).not.toContainText('这一天的地点仍是备选')
  await page.getByTestId('toggle-day-1').click()
  const overview = page.getByTestId('day-overview-1')
  await expect(overview).toContainText('1 个地点待确认。')
  await expect(overview).toContainText('2 个备选地点及方案状态可展开查看。')
  await expect(overview).not.toContainText('尚未加入主线')
  await page.getByTestId('toggle-day-1').click()
  await page.getByTestId('day-alternatives-1').click()
  await expect(group).toContainText('方案A')
  await expect(group).toContainText('已选方案加入行程，地点仍需确认')
  await expect(group.getByRole('button', {name: '未选择此方案', exact: true})).toBeDisabled()
  await page.getByRole('button', {name: '撤销方案选择', exact: true}).click()
  await changed(page, state, 'labeled_cleared')
  expect(state.result.days.map(d => d.activities.map(c => c.name))).toEqual([[], ['西湖']])
  await expect(day).toContainText('这一天的地点仍是备选')
  await expect(group.getByRole('button', {name: '选择方案：方案B', exact: true})).toBeEnabled()
  await page.getByLabel('关闭建议').click()
  await page.getByTestId('undo-trip-command').click()
  await changed(page, state, 'labeled_restored')
  await page.getByTestId('day-alternatives-1').click()
  await expect(group).toContainText('方案A')
  await expect(group).toContainText('已选方案加入行程，地点仍需确认')
  expect(state.result.days.map(d => d.activities.map(c => c.name))).toEqual([['良渚文化村'], ['西湖']])
  expect(state.commands.map(c => c.command_type)).toEqual(['CHOICE_SELECT', 'CHOICE_CLEAR', 'UNDO'])
  await page.screenshot({path: info.outputPath('source-a-b-restored.png'), fullPage: true})
  await info.attach('source-label-command-snapshots', {
    body: JSON.stringify({scope: 'FIXED_PROVIDER_PIPELINE_AND_COMMANDS_ZERO_EXTERNAL_CALLS', ...state}),
    contentType: 'application/json',
  })
})

for (const width of [1440, 390]) test(`source choice select confirm clear and undo at ${width}px`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}, info) => {
  const state = await show(page, width)
  await expect(page.getByLabel('方案加入位置')).toHaveValue('source')
  await page.getByRole('button', {name: '选择方案：方案一', exact: true}).evaluate(button => {button.click(); button.click()})
  await changed(page, state, 'selected')
  expect(state.commands.filter(c => c.command_type === 'CHOICE_SELECT')).toHaveLength(1)
  await expect(page.getByTestId('itinerary-choice-group')).toContainText('地点仍需确认')
  await expect(page.getByTestId('activity-card')).toHaveCount(2)
  await page.getByLabel('关闭建议').click()
  await page.getByTestId('unmatched-places-note').click()
  await page.getByRole('button', {name: '良渚文化村 · 杭州 · 确认地点', exact: true}).click()
  await page.getByTestId('pending-place-dropdown').getByRole('button', {name: '搜索', exact: true}).click()
  await page.getByTestId('pending-place-dropdown').getByRole('button', {name: /良渚文化村/}).click()
  await page.getByRole('button', {name: '使用这个地点', exact: true}).click()
  await changed(page, state, 'confirmed')
  await expect(page.getByTestId('activity-card').getByRole('heading')).toHaveText(['中国美术学院象山校区', '良渚文化村', '西湖'])
  await page.reload()
  await page.getByTestId('day-alternatives-1').click()
  await expect(page.getByTestId('itinerary-choice-group')).toContainText('所选方案已加入行程')
  await page.getByRole('button', {name: '撤销方案选择', exact: true}).click()
  await changed(page, state, 'cleared')
  await page.getByLabel('关闭建议').click()
  await page.getByTestId('undo-trip-command').click()
  await changed(page, state, 'restored')
  await page.getByTestId('day-alternatives-1').click()
  await page.getByRole('button', {name: '撤销方案选择', exact: true}).click()
  await changed(page, state, 'cleared_again')
  await page.getByRole('button', {name: '选择方案：方案二', exact: true}).click()
  await changed(page, state, 'other')
  expect(state.result.days[0].activities.map(c => c.name)).toEqual(['中国美术学院象山校区', '小河直街', '西湖'])
  await page.getByLabel('关闭建议').click()
  await page.getByTestId('undo-trip-command').click()
  await changed(page, state, 'undone_other')
  await page.screenshot({path: info.outputPath('choice-selected-cleared-undo.png'), fullPage: true})
  await info.attach('actual-command-snapshots', {body: JSON.stringify({scope: 'FIXED_PROVIDER_AND_REAL_COMMAND_MUTATIONS_ZERO_NETWORK', ...state}), contentType: 'application/json'})
})

for (const width of [1440, 390]) test(`old source choice has no guessed insertion position at ${width}px`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}) => {
  const state = await show(page, width, 'before', true)
  await expect(page.getByLabel('方案加入位置')).toHaveValue('')
  await expect(page.getByRole('button', {name: '选择方案：方案一', exact: true})).toBeDisabled()
  expect(state.commands).toHaveLength(0)
  await page.getByLabel('方案加入位置').selectOption('1')
  await page.getByRole('button', {name: '选择方案：方案一', exact: true}).click()
  await changed(page, state, 'selected')
})

for (const width of [1440, 390]) test(`modified choice detaches without deleting visits at ${width}px`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}, info) => {
  const state = await show(page, width, 'modified')
  const originalNames = state.result.days.map(d => d.activities.map(c => c.name))
  await expect(page.getByRole('button', {name: '未选择此方案', exact: true})).toBeDisabled()
  await page.getByRole('button', {name: '保留地点，解除方案关联', exact: true}).click()
  await changed(page, state, 'detached')
  expect(state.result.days.map(d => d.activities.map(c => c.name))).toEqual(originalNames)
  expect(state.commands).toHaveLength(1)
  expect(state.commands[0]).toMatchObject({command_type: 'CHOICE_CLEAR', preserve_activities: true})
  expect(state.result.days[0].choice_selections).toEqual([])
  await expect(page.getByRole('button', {name: '选择方案：方案二', exact: true})).toBeEnabled()
  await page.reload()
  expect(state.commands).toHaveLength(1)
  await expect(page.getByTestId('activity-card')).toHaveCount(3)
  await page.getByTestId('undo-trip-command').click()
  await changed(page, state, 'restored_modified')
  await page.getByTestId('day-alternatives-1').click()
  await expect(page.getByRole('button', {name: '未选择此方案', exact: true})).toBeDisabled()
  await page.screenshot({path: info.outputPath('choice-modified-detach-undo.png'), fullPage: true})
})

for (const width of [1440, 390]) test(`complex saved choice permits one manual pending visit without selecting the group at ${width}px`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}, info) => {
  const state = await show(page, width, 'unsupported', false, 3)
  const original = structuredClone(state.result)
  const group = page.getByTestId('itinerary-choice-group')
  await expect(group).toContainText('这组方案需要逐项确认，暂时不能整组加入。')
  await expect(group.getByRole('button', {name: '选择方案：方案A', exact: true})).toBeDisabled()
  await expect(group.getByRole('button', {name: '选择方案：方案B', exact: true})).toBeDisabled()
  await expect(page.getByLabel('方案加入位置')).toHaveCount(0)
  await expect(group.getByRole('button', {name: '加入待确认：静安寺', exact: true})).toBeEnabled()
  await page.screenshot({path: info.outputPath('complex-choice-individual-actions.png')})
  await group.getByRole('button', {name: '加入待确认：静安寺', exact: true}).click()
  await changed(page, state, 'inserted')
  expect(state.commands).toEqual([{
    command_type: 'ALTERNATIVE_INSERT', day_index: 3, position: 0,
    alternative_token: original.days[2].alternatives[0].activity_token,
  }])
  expect(visitContent(state.result.days.slice(0, 2))).toEqual(visitContent(original.days.slice(0, 2)))
  // An originally empty day supplies no before/after anchor. Once a manual
  // stop exists, the command correctly clears the ungrounded default position.
  // No group member or branch identity changes.
  expect(state.result.days[2].alternatives.map(({activity_token, ...a}) => a)).toEqual(original.days[2].alternatives.map(({activity_token, ...a}) => ({...a, insertion_position: null})))
  expect(state.result.days[2].alternatives.every((a, i) => a.activity_token !== original.days[2].alternatives[i].activity_token)).toBe(true)
  expect(state.result.days[2].choice_selections).toEqual([])
  expect(state.result.days[2].activities).toHaveLength(1)
  expect(state.result.days[2].activities[0]).toMatchObject({name: '静安寺', status: 'NEEDS_CONFIRMATION'})
  await page.getByLabel('关闭建议').click()
  await expect(page.getByTestId('activity-card')).toHaveCount(16)
  await page.getByTestId('unmatched-places-note').click()
  await expect(page.getByRole('button', {name: '静安寺 · 确认地点', exact: true})).toBeVisible()
  await page.screenshot({path: info.outputPath('complex-choice-single-pending.png')})
  await page.reload()
  await expect(page.getByTestId('activity-card')).toHaveCount(16)
  await page.getByTestId('day-alternatives-3').click()
  await expect(page.getByTestId('itinerary-choice-group')).toContainText('暂时不能整组加入')
  expect(state.commands).toHaveLength(1)
  await page.getByLabel('关闭建议').click()
  await page.getByTestId('undo-trip-command').click()
  await changed(page, state, 'undone_insert')
  expect(visitContent(state.result.days)).toEqual(visitContent(original.days))
  await page.reload()
  await expect(page.getByTestId('activity-card')).toHaveCount(16)
  expect(state.commands.map(c => c.command_type)).toEqual(['ALTERNATIVE_INSERT', 'UNDO'])
  await info.attach('saved-real-answer-and-real-command-snapshots', {
    body: JSON.stringify({scope: 'SAVED_REAL_MODEL_RESPONSE_FIXED_IDENTITY_ZERO_EXTERNAL_CALLS', ...state}),
    contentType: 'application/json',
  })
})
