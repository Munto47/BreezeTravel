'use client'

import { ChangeEvent, useId, useRef, useState } from 'react'


export const G04_SCREENSHOT_MAX_FILES = 6
export const G04_SCREENSHOT_MAX_FILE_BYTES = 10 * 1024 * 1024

const ALLOWED_MEDIA_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])
const ALLOWED_EXTENSIONS = new Set(['jpeg', 'jpg', 'png', 'webp'])
const UNSAFE_FILENAME_CHARACTERS = /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g
const PATH_SEPARATORS = /[\\/]/g

export type G04ScreenshotSourceState = 'idle' | 'reading' | 'partial' | 'retryable'

export type G04ScreenshotSourceAction =
  | { type: 'add'; count: number }
  | { type: 'move'; from: number; to: number }
  | { type: 'remove'; index: number }
  | { type: 'retry' }

export interface G04ScreenshotSourceProps {
  files: File[]
  onFilesChange: (files: File[]) => void
  onAction?: (action: G04ScreenshotSourceAction) => void
  onRetry?: () => void
  state?: G04ScreenshotSourceState
  disabled?: boolean
  className?: string
}

export type G04ScreenshotFileIssue = {
  code: 'format' | 'size'
  message: string
}

export function safeScreenshotFileName(fileName: string): string {
  const safeName = fileName
    .normalize('NFC')
    .replace(UNSAFE_FILENAME_CHARACTERS, ' ')
    .replace(PATH_SEPARATORS, '_')
    .replace(/\s+/g, ' ')
    .trim()
  const fallback = safeName || '未命名图片'
  const characters = Array.from(fallback)
  if (characters.length <= 96) return fallback
  return `${characters.slice(0, 95).join('')}…`
}

export function getG04ScreenshotFileIssue(file: File): G04ScreenshotFileIssue | null {
  const mediaType = file.type.trim().toLowerCase()
  const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
  const formatAllowed = mediaType
    ? ALLOWED_MEDIA_TYPES.has(mediaType)
    : ALLOWED_EXTENSIONS.has(extension)

  if (!formatAllowed) {
    return {
      code: 'format',
      message: `${safeScreenshotFileName(file.name)} 不是可用的 PNG、JPEG 或 WebP 图片`,
    }
  }
  if (file.size > G04_SCREENSHOT_MAX_FILE_BYTES) {
    return {
      code: 'size',
      message: `${safeScreenshotFileName(file.name)} 超过 10MB，未加入`,
    }
  }
  return null
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const STATE_COPY: Record<G04ScreenshotSourceState, string> = {
  idle: '',
  reading: '正在读取图片，请稍候。',
  partial: '部分图片暂未整理完成，你可以继续使用已完成的内容。',
  retryable: '这次读取未完成，你可以稍后重试。',
}

export default function G04ScreenshotSource({
  files,
  onFilesChange,
  onAction,
  onRetry,
  state = 'idle',
  disabled = false,
  className = '',
}: G04ScreenshotSourceProps) {
  const inputId = useId()
  const helpId = useId()
  const feedbackId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const fileIds = useRef(new WeakMap<File, string>())
  const nextFileId = useRef(0)
  const [localMessage, setLocalMessage] = useState('')
  const boundedFiles = files.slice(0, G04_SCREENSHOT_MAX_FILES)

  const stableFileId = (file: File) => {
    const existing = fileIds.current.get(file)
    if (existing) return existing
    const created = `selected-file-${nextFileId.current}`
    nextFileId.current += 1
    fileIds.current.set(file, created)
    return created
  }

  const handleSelection = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.currentTarget.files ?? [])
    event.currentTarget.value = ''
    if (disabled || selected.length === 0) return

    const valid: File[] = []
    const messages: string[] = []
    selected.forEach(file => {
      const issue = getG04ScreenshotFileIssue(file)
      if (issue) messages.push(issue.message)
      else valid.push(file)
    })

    const availableSlots = Math.max(0, G04_SCREENSHOT_MAX_FILES - boundedFiles.length)
    const accepted = valid.slice(0, availableSlots)
    const overflowCount = valid.length - accepted.length
    if (overflowCount > 0) {
      messages.push(`一次最多选择 6 张，另有 ${overflowCount} 张未加入`)
    }
    setLocalMessage(messages.join('；'))

    if (accepted.length > 0) {
      onFilesChange([...boundedFiles, ...accepted])
      onAction?.({ type: 'add', count: accepted.length })
    }
  }

  const moveFile = (from: number, to: number) => {
    if (disabled || to < 0 || to >= boundedFiles.length) return
    const next = [...boundedFiles]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    setLocalMessage('图片顺序已调整。')
    onFilesChange(next)
    onAction?.({ type: 'move', from, to })
  }

  const removeFile = (index: number) => {
    if (disabled) return
    const next = boundedFiles.filter((_, itemIndex) => itemIndex !== index)
    setLocalMessage('已从本次选择中移除图片。')
    onFilesChange(next)
    onAction?.({ type: 'remove', index })
  }

  const retry = () => {
    if (disabled || !onRetry) return
    onRetry()
    onAction?.({ type: 'retry' })
  }

  const controlledLimitMessage = files.length > G04_SCREENSHOT_MAX_FILES
    ? '当前最多保留 6 张图片，请移除多余图片。'
    : ''
  const feedback = controlledLimitMessage || STATE_COPY[state] || localMessage

  return (
    <section
      className={`rounded-2xl border border-slate-200 bg-white p-4 shadow-sm ${className}`.trim()}
      aria-labelledby={`${inputId}-title`}
      aria-busy={state === 'reading'}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id={`${inputId}-title`} className="text-base font-semibold text-slate-900">
            添加行程截图
          </h2>
          <p id={helpId} className="mt-1 text-sm text-slate-600">
            选择 1–6 张 PNG、JPEG 或 WebP 图片，每张不超过 10MB。
          </p>
        </div>
        <button
          type="button"
          disabled={disabled || boundedFiles.length >= G04_SCREENSHOT_MAX_FILES}
          onClick={() => inputRef.current?.click()}
          aria-controls={inputId}
          className="min-h-12 rounded-xl border border-slate-300 bg-white px-4 text-sm font-medium text-slate-800 shadow-sm transition hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        >
          选择图片
        </button>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
          multiple
          disabled={disabled || boundedFiles.length >= G04_SCREENSHOT_MAX_FILES}
          onChange={handleSelection}
          aria-label="选择行程截图"
          aria-describedby={`${helpId} ${feedbackId}`}
          className="sr-only"
          tabIndex={-1}
        />
      </div>

      {boundedFiles.length > 0 ? (
        <ol className="mt-4 space-y-3" aria-label={`已选择 ${boundedFiles.length} 张图片`}>
          {boundedFiles.map((file, index) => {
            const displayName = safeScreenshotFileName(file.name)
            return (
              <li
                key={stableFileId(file)}
                className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3 sm:flex-row sm:items-center"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-900" title={displayName}>
                    {index + 1}. {displayName}
                  </p>
                  <p className="mt-1 text-xs text-slate-500">{formatFileSize(file.size)}</p>
                </div>
                <div
                  className="flex flex-wrap gap-2"
                  role="group"
                  aria-label={`${displayName} 的操作`}
                >
                  <button
                    type="button"
                    disabled={disabled || index === 0}
                    onClick={() => moveFile(index, index - 1)}
                    aria-label={`将 ${displayName} 上移`}
                    className="min-h-12 min-w-12 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    上移
                  </button>
                  <button
                    type="button"
                    disabled={disabled || index === boundedFiles.length - 1}
                    onClick={() => moveFile(index, index + 1)}
                    aria-label={`将 ${displayName} 下移`}
                    className="min-h-12 min-w-12 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    下移
                  </button>
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => removeFile(index)}
                    aria-label={`移除 ${displayName}`}
                    className="min-h-12 min-w-12 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    删除
                  </button>
                </div>
              </li>
            )
          })}
        </ol>
      ) : (
        <p className="mt-4 rounded-xl bg-slate-50 px-3 py-4 text-sm text-slate-600">
          还没有选择图片。
        </p>
      )}

      <div id={feedbackId} className="mt-3 text-sm text-slate-600" role="status" aria-live="polite">
        {feedback}
      </div>

      {state === 'retryable' && onRetry ? (
        <button
          type="button"
          disabled={disabled}
          onClick={retry}
          className="mt-3 min-h-12 rounded-xl border border-slate-300 bg-white px-4 text-sm font-medium text-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          再试一次
        </button>
      ) : null}
    </section>
  )
}
