import { useEffect, useState } from 'react'
import { getJob } from '../api'

const POLL_INTERVAL_MS = 2000

export default function ProcessingStep({ batchId, onDone, onError }) {
  const [detectJob, setDetectJob] = useState(null)
  const [exportJob, setExportJob] = useState(null)

  useEffect(() => {
    let cancelled = false
    let timer

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
        if (!cancelled) onError(err.message)
      }
    }

    poll()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [batchId, onDone, onError])

  const stageLabel = exportJob
    ? `Stitching output: ${exportJob.stage || 'starting...'}`
    : `Analyzing: ${detectJob?.stage || 'starting...'}`

  return (
    <div>
      <h2>Processing</h2>
      <p>
        <span className="spinner" aria-hidden="true" />
        {stageLabel}
      </p>
      <p style={{ color: '#666', fontSize: 14 }}>
        This can take a while for long recordings. Progress is saved on the
        server, so it's safe to close this tab and come back later -- just
        reopen this page and it'll pick up where it left off.
      </p>
    </div>
  )
}
