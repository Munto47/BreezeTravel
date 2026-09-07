/* Explicit live local measurement. Never run in the fixed CI/browser suites.
 * Each case owns a fresh anonymous context, uses actual UI/API/worker/providers,
 * preserves failures, and deletes only the trip created by that case.
 * Input: a private measure_five_city_recommendations report with cities[].
 */
const fs = require('node:fs')
const path = require('node:path')
const {createHash, randomUUID} = require('node:crypto')
const {execFileSync} = require('node:child_process')
const {chromium, expect} = require('@playwright/test')
const root = path.resolve(__dirname, '../..')
const [inputPath, outputPath, baseURL = 'http://127.0.0.1:3148'] = process.argv.slice(2)
if (!inputPath || !outputPath || !['localhost','127.0.0.1'].includes(new URL(baseURL).hostname))
  throw new Error('Supply an input report, new output file and local experience URL')
const output = path.resolve(outputPath)
if (fs.existsSync(output)) throw new Error('Preserve previous measurements: use a new output file')
const source = JSON.parse(fs.readFileSync(inputPath,'utf8'))
const cases = source.cities.map(c=>({city:c.city,text:c.input_text,expected_days:c.expected_days}))
const folder = output.slice(0,-path.extname(output).length)
fs.mkdirSync(folder,{recursive:true})
function fingerprints() {
  const files={}
  function walk(dir) {
    for (const item of fs.readdirSync(dir,{withFileTypes:true})) {
      const name=path.join(dir,item.name)
      if (item.isDirectory() && !item.name.startsWith('.') && item.name !== '__pycache__') walk(name)
      else if (item.isFile() && /\.(py|jsonl?|md|tsx?|css)$/.test(name))
        files[path.relative(root,name)]=createHash('sha256').update(fs.readFileSync(name)).digest('hex')
    }
  }
  for (const dir of ['backend/app/trip_understanding','frontend/src/app/trip/result','frontend/src/lib']) walk(path.join(root,dir))
  return files
}
const report={schema_version:'local-ui-five-city-v1',provenance:'REAL_BROWSER_LOCAL_API_MODEL_AND_MAP',
  input_provenance:source.input_provenance,acceptance_claim:false,started_at:new Date().toISOString(),
  runtime_before:fingerprints(),cases:[],provider_cost:null,
  limits:['Short developer-constructed inputs; independent POI identity accuracy is evaluated separately.',
    'Times include local API/worker/browser polling; small-sample percentiles are descriptive only.',
    'Browser/API requests do not count model or map HTTP; use worker metrics for provider costs.']}
const save=()=>fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n')
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms))
const shape=body=>body.days.map(d=>({label:d.label,activities:d.activities.map(a=>({name:a.name,category:a.category,status:a.status,meal_role:a.meal_role??null}))}))
async function main() {
  const browser=await chromium.launch({headless:true})
  try {
    for (const [index,input] of cases.entries()) {
      const row={city:input.city,viewport_width:index%2?390:1440,status:'STARTED',checks:{},timings_ms:{},api_writes:{},api_errors:[],expected_days:input.expected_days}
      report.cases.push(row); save()
      const context=await browser.newContext({baseURL,viewport:{width:row.viewport_width,height:950}})
      const page=await context.newPage()
      let resource=null,started=null
      page.on('request',request=>{
        const u=new URL(request.url())
        if (u.origin!==new URL(baseURL).origin || !u.pathname.startsWith('/api/') || !['POST','PUT','PATCH','DELETE'].includes(request.method())) return
        const action=u.pathname.split('/').pop()
        row.api_writes[action]=(row.api_writes[action]||0)+1
      })
      page.on('response',response=>{
        const u=new URL(response.url())
        if (u.origin===new URL(baseURL).origin && u.pathname.startsWith('/api/v3/') && response.status()>=400)
          row.api_errors.push({action:u.pathname.split('/').pop(),status:response.status()})
      })
      const nav=async name=>page.getByTestId(`${row.viewport_width<1024?'mobile':'desktop'}-nav-${name}`).click()
      const read=async suffix=>{
        const response=await page.request.get(`/api/v3/trip-understandings/${resource}${suffix}`)
        return {status:response.status(),etag:response.headers().etag,body:await response.json()}
      }
      const settled=async suffix=>{
        const until=Date.now()+120000
        while (Date.now()<until) {
          const response=await read(suffix)
          if (response.status===200 && !['PROCESSING','PREPARING'].includes(response.body.status)) return response
          if (![200,202].includes(response.status)) throw new Error(`READ_${suffix.replace(/\W/g,'_')}_${response.status}`)
          await wait(1000)
        }
        throw new Error(`TIMEOUT_${suffix.replace(/\W/g,'_')}`)
      }
      try {
        await page.goto('/')
        await page.getByTestId('trip-source-text').fill(input.text)
        started=Date.now()
        await page.getByTestId('create-full-trip').click()
        await page.waitForURL(/\/trip\/result#trip=/,{timeout:20000})
        resource=new URL(page.url()).hash.slice('#trip='.length)
        row.timings_ms.accepted=Date.now()-started
        const firstCard=page.locator('[data-testid="activity-card"],[data-testid="source-lodging-card"],.e-progress-card').first()
        const [,initial]=await Promise.all([
          firstCard.waitFor({timeout:90000}).then(()=>{row.timings_ms.first_visible_card=Date.now()-started}),
          settled('/result').then(result=>{row.timings_ms.final_cards=Date.now()-started;return result}),
        ])
        row.original_days=shape(initial.body)
        row.coverage=initial.body.coverage
        const ready=initial.body.days.flatMap(d=>d.activities).filter(a=>a.status==='READY')
        const wholeTrip=c=>c.category==='住宿' && c.lodging_event==='OVERNIGHT' && c.lodging_scope==='WHOLE_TRIP'
        await expect(page.getByTestId('activity-card')).toHaveCount(ready.filter(c=>!wholeTrip(c)).length)
        await expect(page.getByTestId('source-lodging-card')).toHaveCount(ready.filter(wholeTrip).length)
        row.checks.confirmed_cards_equal_api=true
        row.visible_names=await page.getByTestId('activity-card').getByRole('heading').allTextContents()
        const [dining,stay]=await Promise.all([
          settled('/daily-dining').then(r=>{row.timings_ms.daily_dining=Date.now()-started;return r}),
          settled('/stay-suggestions').then(r=>{row.timings_ms.stay=Date.now()-started;return r}),
        ])
        row.dining={status:dining.body.status,days:dining.body.days.map(d=>({day:d.day_index,status:d.status,area:d.area,
          candidates:d.candidates.map(c=>({name:c.name,address:c.area_or_address,extra_minutes:c.extra_minutes}))}))}
        row.stay={status:stay.body.status,segments:(stay.body.segments||[]).map(s=>({city:s.city,overnight_days:s.overnight_days,
          status:s.status,missing_boundary_count:s.missing_boundary_count,candidates:s.candidates.map(c=>({name:c.name,address:c.area_or_address,brand:c.brand,brand_note:c.brand_note}))}))}
        const firstMeal=dining.body.days.find(d=>d.candidates.length)
        if (firstMeal) await expect(page.getByTestId('daily-meal-card').nth(firstMeal.day_index-1)).toContainText(firstMeal.candidates[0].name,{timeout:15000})
        await page.screenshot({path:path.join(folder,`${index+1}-cards.png`),fullPage:true})
        // Choosing a hotel and a meal both change the version. Exercise lodging
        // on the first case; the following meal suggestions require explicit refresh.
        const availableStay=(stay.body.segments||[]).flatMap(s=>s.candidates).find(c=>!c.selected)
        if (index===0 && availableStay) {
          await nav('map_stay')
          const panel=page.getByTestId('stay-panel')
          await panel.locator('summary').click()
          const beforeMaps=row.api_writes['map-renders']||0
          await panel.getByTestId('choose-stay').first().click()
          await expect(panel.getByTestId('choose-stay').first()).toHaveText('已选择')
          row.checks.stay_selection=true
          row.checks.stay_selection_did_not_recalculate_map=(row.api_writes['map-renders']||0)===beforeMaps
          await nav('itinerary')
          await page.getByRole('button',{name:/撤销/}).first().click()
          await expect.poll(async()=>shape((await read('/result')).body)).toEqual(row.original_days)
          row.checks.stay_undo_preserved_cards=true
          await page.getByTestId('daily-meal-card').first().getByRole('button',{name:'更新用餐建议'}).click()
          await settled('/daily-dining')
        }
        const freshDining=await read('/daily-dining')
        const mealDay=freshDining.body.days?.find(d=>d.candidates?.length)
        if (mealDay && freshDining.body.status==='AVAILABLE') {
          const before=await read('/result')
          const day=before.body.days[mealDay.day_index-1]
          const anchor=day.activities.findIndex(c=>c.activity_token===mealDay.after_activity_token)
          const expectedPosition=anchor+(mealDay.insert_before?0:1)
          const maps=row.api_writes['map-renders']||0
          const panel=page.getByTestId('daily-meal-card').nth(mealDay.day_index-1)
          await panel.getByRole('button',{name:'加入行程'}).first().click()
          await expect(page.getByTestId('activity-card').getByRole('heading',{name:mealDay.candidates[0].name,exact:true})).toBeVisible()
          const adopted=await read('/result')
          row.checks.meal_inserted_at_expected_position=adopted.body.days[mealDay.day_index-1].activities[expectedPosition].name===mealDay.candidates[0].name
          row.checks.meal_is_lunch=adopted.body.days[mealDay.day_index-1].activities[expectedPosition].meal_role==='LUNCH'
          row.checks.meal_edit_did_not_recalculate_map=(row.api_writes['map-renders']||0)===maps
          row.checks.old_daily_suggestions_invalidated=(await read('/daily-dining')).body.status==='NEEDS_UPDATE'
          await page.getByRole('button',{name:/撤销/}).first().click()
          await expect.poll(async()=>shape((await read('/result')).body)).toEqual(shape(before.body))
          row.checks.meal_undo_preserved_cards=true
        } else row.checks.meal_adoption='NOT_AVAILABLE'
        await nav('map_stay')
        const currentMap=await read('/map-renders/latest')
        const currentResult=await read('/result')
        const currentTokens=new Set(currentResult.body.days.flatMap(d=>d.activities).filter(c=>c.status==='READY' && c.lodging_scope!=='WHOLE_TRIP').map(c=>c.activity_token))
        const mappedPoints=currentMap.body.points.filter(p=>p.position && currentTokens.has(p.activity_token))
        const pointCount=mappedPoints.length
        row.map_points_count=pointCount
        expect(pointCount).toBeGreaterThan(0)
        const visibleMap=page.getByTestId('result-view-map-stay').getByTestId('route-map')
        const markers=visibleMap.locator('.e-map-marker:not(.e-map-lodging-marker)')
        await expect(markers).toHaveCount(pointCount,{timeout:20000})
        await expect(visibleMap.getByRole('button',{name:'查看所有地点'})).toBeVisible({timeout:20000})
        await expect(markers.first()).toBeInViewport({timeout:15000})
        row.map_marker_labels=await markers.evaluateAll(items=>items.map(item=>item.getAttribute('aria-label')).sort())
        expect(row.map_marker_labels).toEqual(mappedPoints.map(p=>`查看${p.name}`).sort())
        row.checks.map_markers_match_current_confirmed_points=true
        const lodgingNames=[...new Map((currentMap.body.lodging_points||[]).map(p=>[
          `${p.name}:${p.position.longitude},${p.position.latitude}`,p.name])).values()].sort()
        await expect(visibleMap.locator('.e-map-lodging-marker')).toHaveCount(lodgingNames.length)
        row.map_lodging_labels=await visibleMap.locator('.e-map-lodging-marker').evaluateAll(items=>items.map(item=>item.getAttribute('aria-label')).sort())
        row.checks.map_lodging_markers_match_selected_nights=JSON.stringify(row.map_lodging_labels)===JSON.stringify(lodgingNames.map(n=>`查看住宿位置 ${n}`).sort())
        await page.screenshot({path:path.join(folder,`${index+1}-map-stay.png`),fullPage:true})
        row.status=Object.values(row.checks).includes(false)?'CHECK_FAILED':'MEASURED'
      } catch (error) {
        row.status='FAILED'
        row.failure={type:error.name,step_message:String(error.message).split('\n').slice(0,3).join(' ').slice(0,350)}
        await page.screenshot({path:path.join(folder,`${index+1}-failure.png`),fullPage:true}).catch(()=>{})
      } finally {
        if (resource) {
          if (process.env.EXPERIENCE_PYTHON) {
            try {
              row.worker_metrics=JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON,[path.join(root,'scripts/read_private_measurement.py'),resource],{cwd:root,encoding:'utf8',timeout:15000,windowsHide:true}))
            } catch {row.worker_metrics={status:'READ_FAILED'}}
          } else row.worker_metrics={status:'NOT_CONFIGURED'}
          const response=await page.request.delete(`/api/v3/trip-understandings/${resource}`,{headers:{'Idempotency-Key':randomUUID()}}).catch(()=>null)
          row.cleanup_status=response?.status()??'NETWORK_ERROR'
        }
        await context.close()
        save()
        console.log(JSON.stringify({city:row.city,status:row.status,checks:row.checks,timings_ms:row.timings_ms}))
      }
    }
  } finally {await browser.close()}
  report.runtime_after=fingerprints()
  report.runtime_unchanged=JSON.stringify(report.runtime_before)===JSON.stringify(report.runtime_after)
  report.finished_at=new Date().toISOString()
  report.summary={cases:report.cases.length,measured:report.cases.filter(c=>c.status==='MEASURED').length,failures:report.cases.filter(c=>c.status!=='MEASURED').map(c=>c.city),timings_ms:{}}
  for (const key of ['first_visible_card','final_cards','daily_dining','stay']) {
    const values=report.cases.map(c=>c.timings_ms[key]).filter(Number.isFinite).sort((a,b)=>a-b)
    report.summary.timings_ms[key]={count:values.length,p50:values[Math.max(0,Math.ceil(values.length*.5)-1)]??null,p95:values[Math.max(0,Math.ceil(values.length*.95)-1)]??null}
  }
  save()
}
main().catch(error=>{report.fatal={type:error.name};save();console.error(error.name);process.exitCode=1})
