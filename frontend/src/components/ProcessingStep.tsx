import { useEffect, useState } from 'react'
import { getJob } from '../api'
import type { Job } from '../types'

const POLL_INTERVAL_MS = 2000

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
  onError: (message: string) => void
}

export default function ProcessingStep({ batchId, onDone, onError }: ProcessingStepProps) {
  const [detectJob, setDetectJob] = useState<Job | null>(null)
  const [exportJob, setExportJob] = useState<Job | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>

    async function poll() {
      try {
        const detect = await getJob(batchId, 'detect')
        if (cancelled) return
        setDetectJob(detect)

        if (detect?.status === 'failed') {
          onError(`Detection failed: ${detect.error}`)
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
            onError(`Export failed: ${exp.error}`)
            return
          }
          if (exp?.status === 'interrupted') {
            onError('Export was interrupted (the server restarted mid-run). Please try again.')
            return
          }
        }

        timer = setTimeout(poll, POLL_INTERVAL_MS)
      } catch (err) {
        if (!cancelled) onError(err instanceof Error ? err.message : String(err))
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
      <p>
        <span className="spinner" aria-hidden="true" />
        {label}
        {percent !== null ? ` — ${percent}% through the video` : ''}
      </p>
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
