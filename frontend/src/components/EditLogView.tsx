import { useEffect, useState } from 'react'
import { getJob, getManifest, outputUrl, sourceUrl, triggerExport, updateSegmentStatus } from '../api'
import type { Manifest, Segment } from '../types'
import SkippableVideo from './SkippableVideo'

const EXPORT_POLL_MS = 1000

type LoadState = 'loading' | 'ready' | 'not_ready' | 'error'

// Segments with origin "gap" or "hard_cut" were ever cut by detection
// (see pipeline/manifest.py: origin is set once at build time and never
// changes, independent of status, which restore/un-restore does flip)
// -- so this is the exact, permanent "was this ever a cut candidate"
// marker the spec's "every segment that was cut" listing needs,
// regardless of whether it's since been restored. "hard_cut" specifically
// means real content was destructively trimmed from inside an
// otherwise-kept segment (see README's hard-cut writeup) -- higher risk
// than an ordinary "gap" (motion that never crossed the enter threshold
// at all), which is why it gets its own origin value and its own
// prominent treatment below rather than being listed identically.
function isEditLogEntry(seg: Segment): boolean {
  return seg.origin === 'gap' || seg.origin === 'hard_cut'
}

interface EditLogViewProps {
  batchId: string | null
}

export default function EditLogView({ batchId }: EditLogViewProps) {
  const [manifest, setManifest] = useState<Manifest | null>(null)
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [error, setError] = useState<string | null>(null)
  const [previewingId, setPreviewingId] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(null)
  // Set once an export is confirmed completed (initial load or after a
  // toggle-triggered re-export) -- also doubles as the cache-bust token
  // for the output <video>, so a re-export always shows fresh content
  // instead of the browser serving back the previous export's response
  // for the same URL.
  const [exportVersion, setExportVersion] = useState<string | number | null>(null)
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

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
        setError(err instanceof Error ? err.message : String(err))
        setLoadState('error')
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [batchId])

  // batchId! below (here and in reExport/handleToggle) is safe on the
  // same grounds as the `bid` narrowing further down: these are only
  // ever invoked from the 'ready' render path (a button click, or
  // chained from one), by which point batchId is confirmed non-null --
  // they just can't share that render-scope `const bid` since they're
  // defined earlier in this function body.
  async function waitForExport(): Promise<{ ok: true } | { ok: false; message: string }> {
    // Re-export is stitching only (extract + concat kept spans), not
    // re-running detection -- but "fast" here depends entirely on
    // whether pipeline.stitch's plan needs to re-encode. On a plan with
    // no risky hard-cut boundary it genuinely is ~1s (stream-copy, no
    // real encoding). But ANY hard-cut boundary with real keyframe-snap
    // overlap risk promotes the WHOLE plan to re-encode (see
    // pipeline.stitch.plan_stitch's docstring -- a deliberate safety
    // fix for a real previously-found truncated-export bug, not a
    // shortcut to remove) -- real, live-measured on clip_300 in that
    // state: 16-23s, not ~1s, over 15x the old claim. Root-caused, not
    // just re-measured: profiling isolated it to 12 independent libx264
    // encodes (~94% of total time). run_stitch's extract step now runs
    // those concurrently (max_workers=2 from the backend, the real
    // measured sweet spot -- see pipeline.stitch's docstring for why
    // more workers make it WORSE, not better), a real ~19% improvement,
    // not a full fix: re-encoding itself is still genuinely required for
    // correctness whenever this path triggers, so it's still a real
    // multi-second-to-tens-of-seconds wait, not disappearing. On a real
    // full-length game (~67min), if the same promotion triggers, expect
    // low-single-digit MINUTES, not seconds -- this is still a real
    // background job, so poll rather than assume either way.
    while (true) {
      const job = await getJob(batchId!, 'export')
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
      await triggerExport(batchId!)
      const result = await waitForExport()
      if (result.ok) {
        setExportVersion(Date.now())
      } else {
        setExportError(`Re-export failed: ${result.message}`)
      }
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err))
    } finally {
      setExporting(false)
    }
  }

  async function handleToggle(seg: Segment) {
    const nextStatus = seg.status === 'cut' ? 'kept' : 'cut'
    // Fires the instant the click is registered, before any request
    // goes out -- lets a real click be told apart from a request that
    // fired but is just hard to spot in a busy Network tab.
    console.log(`[EditLog] toggle clicked: segment=${seg.id} status=${seg.status} -> requesting ${nextStatus}`)
    setPendingId(seg.id)
    setError(null)
    try {
      await updateSegmentStatus(batchId!, seg.id, nextStatus)
      // Re-fetch the manifest fresh from the server rather than merging
      // the PATCH response into local state. Whatever was causing the
      // UI to not reflect a real, confirmed-correct server-side change,
      // this removes the whole "local state drifted from server truth"
      // bug class outright: what's rendered is always exactly what the
      // server just reported, not what this component locally believes
      // happened.
      const fresh = await getManifest(batchId!)
      if (fresh) {
        setManifest(fresh)
      } else {
        setError('Segment was updated, but re-fetching the manifest afterward failed.')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
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

  // Both non-null assertions below are guaranteed by the effect above,
  // not just hoped for: 'ready' is only ever set right after
  // setManifest(m) with a real, non-null m, and only once batchId
  // itself was confirmed non-null (the 'not_ready' branch already
  // returned otherwise).
  const activeManifest = manifest!
  const bid = batchId!

  // Hard-cut entries surface first -- they're the higher-risk kind (real
  // content trimmed from inside an otherwise-kept segment, not just
  // ordinary never-flagged dead time) and are the actual safety net for
  // the hard-cut mechanism now: fast, visible, easy to review and
  // restore, not "must never happen" (see README's hard-cut writeup).
  const cutEntries = activeManifest.segments
    .filter(isEditLogEntry)
    .sort((a, b) => (a.origin === 'hard_cut' ? 0 : 1) - (b.origin === 'hard_cut' ? 0 : 1))
  const unreviewedHardCuts = cutEntries.filter(
    (s) => s.origin === 'hard_cut' && s.status === 'cut')

  // The symmetric case cutEntries can't cover: a segment the detector kept
  // from the start (origin=="detected") never became a cut candidate, so
  // isEditLogEntry above -- and therefore the whole cut-review list --
  // never sees it. Without this, a wrongly-kept segment (e.g. a practice-
  // swing clip) has no in-app path to removal at all; confirmed as a real
  // gap during tonight's usability audit, not a hypothetical.
  const keptEntries = activeManifest.segments.filter((seg) => seg.origin === 'detected')

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
            <SkippableVideo
              src={outputUrl(bid, exportVersion)}
              segments={activeManifest.segments}
            />
            <p>
              <a href={outputUrl(bid, exportVersion)} download="highlights.mp4">
                <button className="secondary">Download</button>
              </a>
            </p>
          </>
        ) : (
          !exporting && <p className="muted">No exported output yet.</p>
        )}
      </div>

      {error && <p className="alert alert-danger">{error}</p>}
      {unreviewedHardCuts.length > 0 && (
        <p className="alert alert-warning">
          ⚠ {unreviewedHardCuts.length} segment{unreviewedHardCuts.length === 1 ? '' : 's'}{' '}
          {unreviewedHardCuts.length === 1 ? 'was' : 'were'} automatically cut out of the
          middle of live action, not just detected as dead time -- listed first below.
          Preview and restore any that removed something real.
        </p>
      )}
      {cutEntries.length === 0 ? (
        <p className="muted">Detection didn't cut anything from this video -- nothing to review here.</p>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0 }}>
          {cutEntries.map((seg) => {
            const restored = seg.status === 'kept'
            const isPending = pendingId === seg.id
            const isHardCut = seg.origin === 'hard_cut'
            return (
              <li
                key={seg.id}
                className={`entry-card${isHardCut ? ' hard-cut' : ''}${restored ? ' restored' : ''}`}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                  <div>
                    <strong>
                      {seg.start} - {seg.end}
                    </strong>
                    <span className="muted" style={{ marginLeft: 10 }}>{seg.source_file}</span>
                    {isHardCut && (
                      <span className="badge badge-warning" style={{ marginLeft: 10 }}>
                        ⚠ Auto-cut mid-play — review recommended
                      </span>
                    )}
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
                  <SegmentPreview batchId={bid} segment={seg} />
                )}
              </li>
            )
          })}
        </ul>
      )}

      <div style={{ marginTop: 24, paddingTop: 20, borderTop: '1px solid var(--color-border)' }}>
        <h3 style={{ marginBottom: 8 }}>Kept segments</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Segments the detector kept automatically -- preview and cut any that
          shouldn't be in the highlight reel (e.g. warm-up or practice swings).
        </p>
        {keptEntries.length === 0 ? (
          <p className="muted">No kept segments to review.</p>
        ) : (
          <ul style={{ listStyle: 'none', padding: 0 }}>
            {keptEntries.map((seg) => {
              const isPending = pendingId === seg.id
              const userRemoved = seg.status === 'cut'
              return (
                <li
                  key={seg.id}
                  className={`entry-card${userRemoved ? ' user-removed' : ''}`}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                    <div>
                      <strong>
                        {seg.start} - {seg.end}
                      </strong>
                      <span className="muted" style={{ marginLeft: 10 }}>{seg.source_file}</span>
                      {userRemoved && (
                        <span className="badge badge-neutral" style={{ marginLeft: 10 }}>
                          Removed
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
                        {isPending ? 'Saving...' : userRemoved ? 'Restore' : 'Cut'}
                      </button>
                    </div>
                  </div>
                  {previewingId === seg.id && (
                    <SegmentPreview batchId={bid} segment={seg} />
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}

interface SegmentPreviewProps {
  batchId: string
  segment: Segment
}

function SegmentPreview({ batchId, segment }: SegmentPreviewProps) {
  function handleLoadedMetadata(e: React.SyntheticEvent<HTMLVideoElement>) {
    e.currentTarget.currentTime = segment.start_s
  }

  function handleTimeUpdate(e: React.SyntheticEvent<HTMLVideoElement>) {
    if (e.currentTarget.currentTime >= segment.end_s) {
      e.currentTarget.pause()
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
