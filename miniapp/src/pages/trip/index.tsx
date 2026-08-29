import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Input, Picker, Radio, RadioGroup, ScrollView, Text, View } from '@tarojs/components'
import Taro, { useDidHide, useDidShow, usePullDownRefresh, useRouter } from '@tarojs/taro'

import {
  controlledRunSpec,
  type RepairOptionContract,
  type TripCheckRunContract,
  type WorkspaceResumeContract,
} from '@breezetravel/trip-check-client'

import { tripCheckClient } from '@/lib/api'
import { commandRegistry } from '@/lib/commands'
import { pollDelay, shouldPollRun } from '@/lib/polling'
import { readSession, saveLastWorkspaceId } from '@/lib/storage'
import { canResumeRun, canStartTripCheck } from '@/lib/workflow'

import './index.scss'

const TRAVELER_OPTIONS = [2, 3, 4, 5]

export default function TripPage() {
  const router = useRouter()
  const workspaceId = String(router.params.workspaceId || '')
  const [resume, setResume] = useState<WorkspaceResumeContract | null>(null)
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [queries, setQueries] = useState<Record<string, string>>({})
  const [travelerCount, setTravelerCount] = useState(2)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const visible = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const itineraryImport = resume?.current_import || null
  const brief = resume?.current_brief || null
  const run = resume?.current_trip_check_run || null
  const report = resume?.current_report || null
  const advice = resume?.current_advice || null
  const repairs = useMemo(() => [
    ...(resume?.proposed_repairs || []),
    ...(resume?.applied_repair ? [resume.applied_repair] : []),
  ], [resume])
  const unresolved = useMemo(
    () => (itineraryImport?.resolutions || []).filter(item => ['AMBIGUOUS', 'NOT_FOUND'].includes(item.resolution_status)),
    [itineraryImport],
  )

  const stopName = (rawStopId: string) => (
    itineraryImport?.raw_stops?.find(item => item.raw_stop_id === rawStopId)?.raw_name || rawStopId
  )

  const clearTimer = () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
  }

  const loadAuthoritative = async (): Promise<WorkspaceResumeContract> => {
    if (!workspaceId) throw new Error('缺少 workspaceId')
    const restored = await tripCheckClient.resumeWorkspace(workspaceId)
    setResume(restored)
    setTravelerCount(restored.current_brief?.traveler_count || 2)
    saveLastWorkspaceId(workspaceId)
    return restored
  }

  const poll = (currentRun: TripCheckRunContract, failureCount = 0) => {
    clearTimer()
    if (!visible.current || !shouldPollRun(currentRun.status, currentRun.stage)) return
    timer.current = setTimeout(async () => {
      if (!visible.current) return
      try {
        const next = await tripCheckClient.getRun(currentRun.run_id)
        setResume(current => current ? { ...current, current_trip_check_run: next } : current)
        if (!shouldPollRun(next.status, next.stage)) {
          const restored = await loadAuthoritative()
          if (restored.current_trip_check_run) poll(restored.current_trip_check_run)
          return
        }
        poll(next, 0)
      } catch (caught) {
        setError(`Run 状态读取失败，正在退避重试：${caught instanceof Error ? caught.message : String(caught)}`)
        poll(currentRun, failureCount + 1)
      }
    }, pollDelay(failureCount))
  }

  const refresh = async () => {
    setError('')
    try {
      const restored = await loadAuthoritative()
      if (restored.current_trip_check_run) poll(restored.current_trip_check_run)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  useDidShow(() => {
    visible.current = true
    if (!readSession()) {
      void Taro.reLaunch({ url: '/pages/login/index' })
      return
    }
    void refresh()
  })
  useDidHide(() => {
    visible.current = false
    clearTimer()
  })
  usePullDownRefresh(() => {
    void refresh().finally(() => Taro.stopPullDownRefresh())
  })
  useEffect(() => () => clearTimer(), [])

  const act = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    setError('')
    try {
      await action()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy('')
    }
  }

  const saveAndConfirmBrief = () => act('brief', async () => {
    if (!brief) return
    let target = brief
    if (travelerCount !== brief.traveler_count) {
      const scope = `patch-brief:${brief.brief_id}:${brief.revision}`
      target = await tripCheckClient.patchBrief(
        workspaceId,
        brief.revision,
        { traveler_count: travelerCount },
        commandRegistry.acquire(scope, { traveler_count: travelerCount }),
      )
      commandRegistry.complete(scope)
    }
    const scope = `confirm-brief:${target.brief_id}:${target.revision}`
    await tripCheckClient.confirmBrief(workspaceId, target.revision, commandRegistry.acquire(scope, {}))
    commandRegistry.complete(scope)
    await loadAuthoritative()
  })

  const search = (rawStopId: string) => act(`search:${rawStopId}`, async () => {
    if (!itineraryImport || !queries[rawStopId]?.trim()) return
    await tripCheckClient.searchCandidates(
      workspaceId,
      itineraryImport.import_id,
      rawStopId,
      queries[rawStopId].trim(),
      itineraryImport.state_version,
    )
    await loadAuthoritative()
  })

  const confirmPlaces = () => act('places', async () => {
    if (!itineraryImport) return
    const confirmations = unresolved.map(item => ({ raw_stop_id: item.raw_stop_id, place_id: selections[item.raw_stop_id] }))
    if (confirmations.some(item => !item.place_id)) throw new Error('每个待消歧地点都必须明确选择，不能静默应用')
    await tripCheckClient.confirmResolutions(
      workspaceId,
      itineraryImport.import_id,
      { confirmations },
      itineraryImport.state_version,
    )
    await loadAuthoritative()
  })

  const startRun = () => act('run', async () => {
    if (!brief || brief.status !== 'CONFIRMED') throw new Error('TripBrief 尚未确认')
    let revision = resume?.current_revision || null
    if (!revision) {
      if (!itineraryImport || itineraryImport.status !== 'READY') throw new Error('地点消歧尚未完成')
      const scope = `apply-import:${itineraryImport.import_id}`
      const applied = await tripCheckClient.applyImport(workspaceId, itineraryImport, commandRegistry.acquire(scope, { version: itineraryImport.state_version }))
      commandRegistry.complete(scope)
      revision = applied.revision
    }
    const body = {
      itinerary_revision: revision.revision,
      brief_revision: brief.revision,
      run_spec: controlledRunSpec(process.env.TARO_APP_TRIP_CHECK_COMMIT_SHA || 'miniapp-local'),
    }
    const scope = `create-run:${workspaceId}:${revision.revision}:${brief.revision}`
    const created = await tripCheckClient.createRun(workspaceId, body, commandRegistry.acquire(scope, body))
    commandRegistry.complete(scope)
    const restored = await loadAuthoritative()
    poll(restored.current_trip_check_run || created)
  })

  const resumeRun = () => act('resume-run', async () => {
    if (!run) return
    const scope = `resume-run:${run.run_id}:${run.version}`
    const next = await tripCheckClient.resumeRun(run, commandRegistry.acquire(scope, { config_hash: run.config_hash }))
    commandRegistry.complete(scope)
    setResume(current => current ? { ...current, current_trip_check_run: next } : current)
    poll(next)
  })

  const proposeRepairs = () => act('repairs', async () => {
    if (!report) return
    const scope = `propose-repairs:${report.report_id}`
    await tripCheckClient.proposeRepairs(report.report_id, commandRegistry.acquire(scope, {}))
    commandRegistry.complete(scope)
    await loadAuthoritative()
  })

  const applyRepair = (option: RepairOptionContract) => act(option.repair_id, async () => {
    const scope = `apply-repair:${option.repair_id}`
    const result = await tripCheckClient.applyRepair(option, commandRegistry.acquire(scope, { base_revision: option.base_itinerary_revision }))
    commandRegistry.complete(scope)
    await tripCheckClient.getAudit(result.postcheck_report_id)
    await loadAuthoritative()
  })

  const rejectRepair = (option: RepairOptionContract) => act(option.repair_id, async () => {
    await tripCheckClient.rejectRepair(option, '小程序用户暂不采纳')
    await loadAuthoritative()
  })

  if (!resume) return <View className='page loading'><Text>{error || '正在恢复权威状态…'}</Text></View>

  return (
    <ScrollView scrollY className='page trip-page'>
      <View className='hero'>
        <Text className='eyebrow'>行程查 · {resume.workspace.city}</Text>
        <Text className='heading'>{resume.workspace.trip_date_range.start} 至 {resume.workspace.trip_date_range.end}</Text>
        <Text className='subtle'>Workspace {resume.workspace.workspace_id.slice(0, 8)}</Text>
      </View>
      {error ? <Text className='error'>{error}</Text> : null}

      {brief ? <View className='card section'>
        <View className='section-head'><Text className='section-title'>1. Brief 确认</Text><Text className={`badge ${brief.status.toLowerCase()}`}>{brief.status}</Text></View>
        <Text className='copy'>城市 {brief.city} · {brief.date_range.start} 至 {brief.date_range.end}</Text>
        <Text className='label'>出行人数</Text>
        <Picker disabled={brief.status === 'CONFIRMED'} mode='selector' range={TRAVELER_OPTIONS} value={Math.max(0, TRAVELER_OPTIONS.indexOf(travelerCount))} onChange={event => setTravelerCount(TRAVELER_OPTIONS[Number(event.detail.value)])}>
          <View className='field'>{travelerCount} 人</View>
        </Picker>
        <Text className='copy'>节奏 {brief.daily_pace} · 强度 {brief.activity_intensity} · 交通 {(brief.transport_modes || []).join('、') || '未提供'}</Text>
        {brief.status !== 'CONFIRMED' ? <Button className='primary' loading={busy === 'brief'} onClick={saveAndConfirmBrief}>保存并确认 Brief</Button> : null}
      </View> : null}

      {unresolved.length ? <View className='card section'>
        <Text className='section-title'>2. 地点消歧</Text>
        <Text className='copy'>低置信度地点必须逐项确认。</Text>
        {unresolved.map(item => <View className='resolution' key={item.raw_stop_id}>
          <Text className='resolution-name'>{stopName(item.raw_stop_id)}</Text>
          <RadioGroup onChange={event => setSelections(current => ({ ...current, [item.raw_stop_id]: event.detail.value }))}>
            {(item.candidates || []).map(candidate => <View className='candidate' key={candidate.place_id}>
              <Radio value={candidate.place_id} checked={selections[item.raw_stop_id] === candidate.place_id} color='#f06f5f' />
              <View className='candidate-copy'><Text>{candidate.name}</Text><Text className='subtle'>{candidate.address || candidate.district || candidate.city}</Text></View>
            </View>)}
          </RadioGroup>
          <View className='search-row'>
            <Input className='search-input' value={queries[item.raw_stop_id] || ''} placeholder='换一个检索词' onInput={event => setQueries(current => ({ ...current, [item.raw_stop_id]: event.detail.value }))} />
            <Button className='small' onClick={() => search(item.raw_stop_id)}>搜索</Button>
          </View>
        </View>)}
        <Button className='primary' loading={busy === 'places'} onClick={confirmPlaces}>确认全部地点</Button>
      </View> : null}

      {canStartTripCheck(brief, itineraryImport, Boolean(resume.current_revision)) && !run ? <View className='card section'>
        <Text className='section-title'>3. 开始权威核验</Text>
        <Text className='copy'>将创建或复用当前 Itinerary Revision，并启动完整事实采集、Audit 与 Advice。</Text>
        <Button className='primary' loading={busy === 'run'} onClick={startRun}>开始 Run</Button>
      </View> : null}

      {run ? <View className='card section'>
        <View className='section-head'><Text className='section-title'>Run 进度</Text><Text className={`badge ${run.status.toLowerCase()}`}>{run.status}</Text></View>
        <Text className='copy'>阶段 {run.stage} · 版本 {run.version}</Text>
        <View className='progress'><View className='progress-fill' style={{ width: `${Math.min(100, ((run.completed_stages?.length || 0) / 8) * 100)}%` }} /></View>
        {(run.partial_failures || []).map((failure, index) => <Text className='warning' key={`${failure.stage}-${index}`}>PARTIAL · {failure.stage} · {failure.category}</Text>)}
        {canResumeRun(run) ? <Button className='secondary compact' loading={busy === 'resume-run'} onClick={resumeRun}>从权威状态恢复 Run</Button> : null}
      </View> : null}

      {report ? <View className='card section'>
        <View className='section-head'><Text className='section-title'>报告与 Advice</Text><Text className={`badge ${report.overall_status.toLowerCase()}`}>{report.overall_status}</Text></View>
        {(report.findings || []).length ? (report.findings || []).map(finding => <View className='finding' key={finding.finding_id}>
          <Text className='finding-title'>{finding.severity} · {finding.status}</Text>
          <Text className='copy'>{finding.message}</Text>
          {finding.confirmation_action ? <Text className='subtle'>建议动作：{finding.confirmation_action}</Text> : null}
        </View>) : <Text className='copy'>当前报告没有发现项。</Text>}
        {(advice?.actions || []).map(action => <View className='advice' key={action.advice_id}><Text className='finding-title'>建议</Text><Text className='copy'>{action.action}</Text><Text className='subtle'>{action.expected_impact} · {action.uncertainty}</Text></View>)}
        {!repairs.length && run?.stage === 'WAIT_ADOPTION' ? <Button className='primary' loading={busy === 'repairs'} onClick={proposeRepairs}>生成 Repair 预览</Button> : null}
      </View> : null}

      {repairs.length ? <View className='card section'>
        <Text className='section-title'>Repair 预览与 postcheck</Text>
        {repairs.map(option => <View className='repair' key={option.repair_id}>
          <View className='section-head'><Text className='finding-title'>方案 {option.repair_id.slice(0, 8)}</Text><Text className='badge'>{option.status}</Text></View>
          {(option.tradeoffs || []).map(item => <Text className='copy' key={item}>· {item}</Text>)}
          <Text className='subtle'>编辑成本 {option.edit_cost} · 风险成本 {option.risk_cost} · 基于 r{option.base_itinerary_revision}</Text>
          {option.status === 'PROPOSED' ? <View className='button-row'>
            <Button className='primary half' loading={busy === option.repair_id} onClick={() => applyRepair(option)}>采纳并完整 postcheck</Button>
            <Button className='secondary half' onClick={() => rejectRepair(option)}>暂不采纳</Button>
          </View> : null}
          {option.postcheck_report_id ? <Text className='success'>已绑定 postcheck {option.postcheck_report_id.slice(0, 10)}</Text> : null}
        </View>)}
      </View> : null}
      <View className='bottom-space' />
    </ScrollView>
  )
}
