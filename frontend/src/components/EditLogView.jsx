import { useEffect, useState } from 'react'
import { getJob, getManifest, outputUrl, sourceUrl, triggerExport, updateSegmentStatus } from '../api'
import SkippableVideo from './SkippableVideo'

const EXPORT_POLL_MS = 1000

// Only segments with origin "gap" were ever cut by detection (see
// pipeline/manifest.py: origin is set once at build time and never
// changes, independent of status, which restore/un-restore does flip)
// -- so this is the exact, permanent "was this ever a cut candidate"
// marker the spec's "every segment that was cut" listing needs,
// regardless of whether it's since been restored.
function isEditLogEntry(seg) {
  return seg.origin === 'gap'
}

export default function EditLogView({ batchId }) {
  const [manifest, setManifest] = useState(null)
  const [loadState, setLoadState] = useState('loading') // loading | ready | not_ready | error
  const [error, setError] = useState(null)
  const [previewingId, setPreviewingId] = useState(null)
  const [pendingId, setPendingId] = useState(null)
  // Set once an export is confirmed completed (initial load or after a
  // toggle-triggered re-export) -- also doubles as the cache-bust token
  // for the output <video>, so a re-export always shows fresh content
  // instead of the browser serving back the previous export's response
  // for the same URL.
  const [exportVersion, setExportVersion] = useState(null)
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      if (!batchId) {
        setLoadState('not_ready')
        return
      }
      setLoadState('loading')
      try {
        const m = await getManifest(batchId)
        if (cancelled) return
        if (!m) {
          setLoadState('not_ready')
          return
        }
        setManifest(m)
        setLoadState('ready')
        // Surface whatever output already exists (e.g. Stage 7's
        // auto-chained export from the original processing run) --
        // the player shouldn't only appear after a restore this session.
        const exp = await getJob(batchId, 'export')
        if (!cancelled && exp?.status === 'completed') {
          setExportVersion(exp.updated_at || Date.now())
        }
      } catch (err) {
        if (cancelled) return
        setError(err.message)
        setLoadState('error')
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [batchId])

  async function waitForExport() {
    // Re-export is stitching only (extract + concat kept spans), not
    // re-running detection, so this is expected to be fast in practice
    // (measured: ~1s on clip_300) -- but it's still a real background
    // job, so poll rather than assume.
    while (true) {
      const job = await getJob(batchId, 'export')
      if (job?.status === 'completed') return { ok: true }
      if (job?.status === 'failed' || job?.status === 'interrupted') {
        return { ok: false, message: job.error || `export ${job.status}` }
      }
      await new Promise((resolve) => setTimeout(resolve, EXPORT_POLL_MS))
    }
  }

  async function reExport() {
    setExporting(true)
    setExportError(null)
    try {
      await triggerExport(batchId)
      const result = await waitForExport()
      if (result.ok) {
        setExportVersion(Date.now())
      } else {
        setExportError(`Re-export failed: ${result.message}`)
      }
    } catch (err) {
      setExportError(err.message)
    } finally {
      setExporting(false)
    }
  }

  async function handleToggle(seg) {
    const nextStatus = seg.status === 'cut' ? 'kept' : 'cut'
    // Fires the instant the click is registered, before any request
    // goes out -- lets a real click be told apart from a request that
    // fired but is just hard to spot in a busy Network tab.
    console.log(`[EditLog] toggle clicked: segment=${seg.id} status=${seg.status} -> requesting ${nextStatus}`)
    setPendingId(seg.id)
    setError(null)
    try {
      await updateSegmentStatus(batchId, seg.id, nextStatus)
      // Re-fetch the manifest fresh from the server rather than merging
      // the PATCH response into local state. Whatever was causing the
      // UI to not reflect a real, confirmed-correct server-side change,
      // this removes the whole "local state drifted from server truth"
      // bug class outright: what's rendered is always exactly what the
      // server just reported, not what this component locally believes
      // happened.
      const fresh = await getManifest(batchId)
      if (fresh) {
        setManifest(fresh)
      } else {
        setError('Segment was updated, but re-fetching the manifest afterward failed.')
      }
    } catch (err) {
      setError(err.message)
      return
    } finally {
      setPendingId(null)
    }
    // The status change succeeded -- now make the final video actually
    // reflect it. Runs for both directions (restore AND cut-again): a
    // toggle that updates the manifest but leaves the last-exported
    // video showing the old state would be a half-finished feature.
    await reExport()
  }

  if (loadState === 'loading') {
    return (
      <div className="card">
        <span className="spinner" aria-hidden="true" />
        Loading...
      </div>
    )
  }

  if (loadState === 'not_ready') {
    return (
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Edit Log</h2>
        <p className="muted">
          No processed video yet for this session -- upload and process a
          video from the Home tab first.
        </p>
      </div>
    )
  }

  if (loadState === 'error') {
    return (
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Edit Log</h2>
        <p className="alert alert-danger">{error}</p>
      </div>
    )
  }

  const cutEntries = manifest.segments.filter(isEditLogEntry)

  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Edit Log</h2>

      <div style={{ marginBottom: 24, paddingBottom: 20, borderBottom: '1px solid var(--color-border)' }}>
        <h3 style={{ marginBottom: 8 }}>Current output</h3>
        {exporting && (
          <p>
            <span className="spinner" aria-hidden="true" />
            Re-exporting output...
          </p>
        )}
        {exportError && <p className="alert alert-danger">{exportError}</p>}
        {exportVersion ? (
          <>
            {/* Skip-ahead suggestions temporarily disabled (not passing
                segments) -- see the comment in ResultStep.jsx and README
                Known limitations: computeSkipWindows' cumulative-duration
                mapping was confirmed to diverge from the real rendered
                output by up to 17s on a real batch. */}
            <SkippableVideo
              src={outputUrl(batchId, exportVersion)}
            />
            <p>
              <a href={outputUrl(batchId, exportVersion)} download="highlights.mp4">
                <button className="secondary">Download</button>
              </a>
            </p>
          </>
        ) : (
          !exporting && <p className="muted">No exported output yet.</p>
        )}
      </div>

      {error && <p className="alert alert-danger">{error}</p>}
      {cutEntries.length === 0 ? (
        <p className="muted">Detection didn't cut anything from this video -- nothing to review here.</p>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0 }}>
          {cutEntries.map((seg) => {
            const restored = seg.status === 'kept'
            const isPending = pendingId === seg.id
            return (
              <li key={seg.id} className={`entry-card${restored ? ' restored' : ''}`}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                  <div>
                    <strong>
                      {seg.start} - {seg.end}
                    </strong>
                    <span className="muted" style={{ marginLeft: 10 }}>{seg.source_file}</span>
                    {restored && (
                      <span className="badge badge-success" style={{ marginLeft: 10 }}>
                        Restored
                      </span>
                    )}
                  </div>
                  <div>
                    <button
                      className="secondary"
                      onClick={() => setPreviewingId(previewingId === seg.id ? null : seg.id)}
                      style={{ marginRight: 8 }}
                    >
                      {previewingId === seg.id ? 'Hide preview' : 'Preview'}
                    </button>
                    <button onClick={() => handleToggle(seg)} disabled={isPending || exporting}>
                      {isPending ? 'Saving...' : restored ? 'Cut again' : 'Restore'}
                    </button>
                  </div>
                </div>
                {previewingId === seg.id && (
                  <SegmentPreview batchId={batchId} segment={seg} />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function SegmentPreview({ batchId, segment }) {
  function handleLoadedMetadata(e) {
    e.target.currentTime = segment.start_s
  }

  function handleTimeUpdate(e) {
    if (e.target.currentTime >= segment.end_s) {
      e.target.pause()
    }
  }

  return (
    <video
      controls
      src={sourceUrl(batchId, segment.source_file)}
      onLoadedMetadata={handleLoadedMetadata}
      onTimeUpdate={handleTimeUpdate}
      style={{ maxWidth: '100%', width: '100%', background: '#000', marginTop: 10 }}
    />
  )
}
