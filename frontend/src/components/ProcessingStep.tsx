import { useEffect, useRef, useState } from 'react'
import { AppError, type AppErrorKind, classifyMessage, getJob } from '../api'
import type { Job } from '../types'

const POLL_INTERVAL_MS = 2000
// A real transient backend blip (a --reload restart picking up a code
// change, a container restarting, a brief network hiccup) is exactly
// the kind of thing a developer waits out without a second thought but
// a non-technical volunteer reads as "it's broken, start over" -- see
// docs/INVESTIGATION_LOG.md's real ECONNREFUSED incident. Tolerating a
// handful of consecutive failed polls before giving up (10s at the
// current interval) means the UI rides out a brief outage instead of
// ending the session on the very first missed poll.
const MAX_CONSECUTIVE_NETWORK_FAILURES = 5

// Human labels for the raw stage names pipeline/run.py and
// pipeline/stitch.py report via on_stage. Falls back to the raw stage
// text itself for anything not listed here, so an unrecognized future
// stage still shows something real rather than going blank.
const STAGE_LABELS: Record<string, string> = {
  'analyzing motion': 'Analyzing motion',
  'running player detection': 'Detecting plays',
  'extending and padding segments': 'Refining play boundaries',
  'building manifest': 'Finalizing analysis',
  'extracting kept segments': 'Cutting downtime',
  'stitching output': 'Assembling final video',
}

// Parses a raw job.stage string like
// "running player detection (612s/4050s) (full_game.mkv)" into a clean
// label plus a real percent, when the backend actually reports one.
// Only `analyzing motion` and `running player detection` currently embed
// a real (secondsDone/secondsTotal) fraction (see pipeline/run.py) --
// every other stage has no per-frame signal to report, so this
// deliberately returns percent: null for those rather than guessing one.
function parseStage(raw: string | null | undefined): { label: string; percent: number | null } {
  if (!raw) return { label: 'starting...', percent: null }

  const base = raw.split('(')[0].trim()
  const label = STAGE_LABELS[base] ?? base

  const match = raw.match(/\((\d+)s\/(\d+)s\)/)
  if (!match) return { label, percent: null }

  const done = Number(match[1])
  const total = Number(match[2])
  if (!total) return { label, percent: null }
  const percent = Math.min(100, Math.max(0, Math.round((done / total) * 100)))
  return { label, percent }
}

interface ProcessingStepProps {
  batchId: string
  onDone: () => void
  onError: (message: string, kind?: AppErrorKind) => void
}

export default function ProcessingStep({ batchId, onDone, onError }: ProcessingStepProps) {
  const [detectJob, setDetectJob] = useState<Job | null>(null)
  const [exportJob, setExportJob] = useState<Job | null>(null)
  // Visible while tolerating consecutive network failures, below --
  // distinct from the normal "Working" state so a real outage isn't
  // silently indistinguishable from ordinary processing until it
  // suddenly gives up.
  const [reconnecting, setReconnecting] = useState(false)
  // A ref, not state: read/written from inside the poll loop's closure
  // on every attempt, and a state update here would just cause a render
  // this component doesn't otherwise need.
  const consecutiveNetworkFailures = useRef(0)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>

    async function poll() {
      try {
        const detect = await getJob(batchId, 'detect')
        if (cancelled) return
        consecutiveNetworkFailures.current = 0
        setReconnecting(false)
        setDetectJob(detect)

        if (detect?.status === 'failed') {
          const classified = classifyMessage(detect.error || '')
          onError(`Detection failed: ${classified.message}`, classified.kind)
          return
        }
        if (detect?.status === 'interrupted') {
          onError('Detection was interrupted (the server restarted mid-run). Please try again.')
          return
        }

        if (detect?.status === 'completed') {
          // auto-chained by the backend right after detect -- the
          // export job file may not exist for a brief moment yet
          // (created a beat after detect flips to completed), so a
          // null here just means "keep polling", not an error
          const exp = await getJob(batchId, 'export')
          if (cancelled) return
          setExportJob(exp)

          if (exp?.status === 'completed') {
            onDone()
            return
          }
          if (exp?.status === 'failed') {
            const classified = classifyMessage(exp.error || '')
            onError(`Export failed: ${classified.message}`, classified.kind)
            return
          }
          if (exp?.status === 'interrupted') {
            onError('Export was interrupted (the server restarted mid-run). Please try again.')
            return
          }
        }

        timer = setTimeout(poll, POLL_INTERVAL_MS)
      } catch (err) {
        if (cancelled) return
        const isNetwork = err instanceof AppError && err.kind === 'network'
        if (isNetwork && consecutiveNetworkFailures.current < MAX_CONSECUTIVE_NETWORK_FAILURES) {
          consecutiveNetworkFailures.current += 1
          setReconnecting(true)
          timer = setTimeout(poll, POLL_INTERVAL_MS)
          return
        }
        onError(
          err instanceof Error ? err.message : String(err),
          err instanceof AppError ? err.kind : 'server',
        )
      }
    }

    poll()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [batchId, onDone, onError])

  const { label, percent } = parseStage(exportJob ? exportJob.stage : detectJob?.stage)

  return (
    <div className="card">
      <span className="step-eyebrow">Working</span>
      <h2 style={{ marginTop: 0 }}>Processing</h2>
      {reconnecting ? (
        <p className="alert alert-warning">
          <span className="spinner" aria-hidden="true" />
          Lost the connection to the server -- retrying...
        </p>
      ) : (
        <p>
          <span className="spinner" aria-hidden="true" />
          {label}
          {percent !== null ? ` — ${percent}% through the video` : ''}
        </p>
      )}
      <div className="progress-track">
        {percent !== null ? (
          <div className="progress-fill" style={{ width: `${percent}%` }} />
        ) : (
          <div className="progress-fill progress-fill--indeterminate" />
        )}
      </div>
      <p className="muted" style={{ fontSize: 14 }}>
        This can take a while for long recordings. Progress is saved on the
        server, so it's safe to close this tab and come back later -- just
        reopen this page and it'll pick up where it left off.
      </p>
    </div>
  )
}
