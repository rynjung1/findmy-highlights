import { useEffect, useRef, useState } from 'react'
import {
  AppError,
  classifyMessage,
  getJob,
  getManifest,
  outputUrl,
  sourceUrl,
  triggerExport,
  updateSegmentStatus,
} from '../api'
import type { Manifest, Segment } from '../types'
import SkippableVideo, { type SkippableVideoHandle } from './SkippableVideo'

const EXPORT_POLL_MS = 1000
// Same tolerance as ProcessingStep's detect-stage polling, and for the
// same real reason: a brief backend blip mid-re-export shouldn't end
// the attempt on the very first missed poll.
const MAX_CONSECUTIVE_NETWORK_FAILURES = 5

// Segment-relative duration, not two timestamps the viewer has to
// subtract themselves -- real usability gap flagged in tonight's Edit
// Log audit, especially at full-game scale (hundreds of entries).
function formatDuration(seconds: number): string {
  if (seconds < 60) {
    return `${seconds.toFixed(seconds < 10 ? 2 : 1)}s`
  }
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds - m * 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

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
  // For the "jump to this point" affordance (finding #2 of tonight's
  // Edit Log audit): outputVideoRef drives the actual seek+play,
  // outputSectionRef just scrolls the player into view first so a jump
  // from an entry far down a long list is visible, not silent.
  const outputVideoRef = useRef<SkippableVideoHandle>(null)
  const outputSectionRef = useRef<HTMLDivElement>(null)

  function handleJumpToOutput(seconds: number) {
    outputSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    outputVideoRef.current?.seekTo(seconds)
  }

  // "Jump to next flagged" (hard-cut) entry -- lets a reviewer page
  // through just the higher-risk hard-cut segments without scrolling
  // past dozens of ordinary gap cuts to find them. Cursor tracks
  // position within the FULL hard-cut list (not just still-unreviewed
  // ones), so it stays usable even after every flagged segment has
  // already been restored/confirmed -- a reviewer paging through to
  // double-check shouldn't lose the control the moment the count hits
  // zero.
  const [flaggedCursor, setFlaggedCursor] = useState(-1)
  const [highlightedId, setHighlightedId] = useState<string | null>(null)
  const highlightTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  function handleJumpToNextFlagged(flaggedEntries: Segment[]) {
    if (flaggedEntries.length === 0) return
    const next = (flaggedCursor + 1) % flaggedEntries.length
    setFlaggedCursor(next)
    const target = flaggedEntries[next]
    document.getElementById(`edit-log-entry-${target.id}`)?.scrollIntoView({
      behavior: 'smooth',
      block: 'center',
    })
    if (highlightTimeoutRef.current) clearTimeout(highlightTimeoutRef.current)
    setHighlightedId(target.id)
    highlightTimeoutRef.current = setTimeout(() => setHighlightedId(null), 1600)
  }

  useEffect(() => {
    return () => {
      if (highlightTimeoutRef.current) clearTimeout(highlightTimeoutRef.current)
    }
  }, [])

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
    let consecutiveNetworkFailures = 0
    while (true) {
      let job
      try {
        job = await getJob(batchId!, 'export')
        consecutiveNetworkFailures = 0
      } catch (err) {
        const isNetwork = err instanceof AppError && err.kind === 'network'
        if (isNetwork && consecutiveNetworkFailures < MAX_CONSECUTIVE_NETWORK_FAILURES) {
          consecutiveNetworkFailures += 1
          await new Promise((resolve) => setTimeout(resolve, EXPORT_POLL_MS))
          continue
        }
        return { ok: false, message: err instanceof Error ? err.message : String(err) }
      }
      if (job?.status === 'completed') return { ok: true }
      if (job?.status === 'failed' || job?.status === 'interrupted') {
        return { ok: false, message: classifyMessage(job.error || `export ${job.status}`).message }
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
  // Every hard-cut entry, reviewed or not -- what "Jump to next flagged"
  // cycles through (see the handler above for why this is broader than
  // unreviewedHardCuts).
  const flaggedEntries = cutEntries.filter((s) => s.origin === 'hard_cut')

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

      <div ref={outputSectionRef} style={{ marginBottom: 24, paddingBottom: 20, borderBottom: '1px solid var(--color-border)' }}>
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
              ref={outputVideoRef}
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
      {flaggedEntries.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
          <button type="button" className="secondary" onClick={() => handleJumpToNextFlagged(flaggedEntries)}>
            ↓ Next flagged
          </button>
          <span className="muted" style={{ fontSize: 13 }}>
            {flaggedCursor >= 0
              ? `${flaggedCursor + 1} of ${flaggedEntries.length} flagged segments`
              : `${flaggedEntries.length} flagged segment${flaggedEntries.length === 1 ? '' : 's'} in this list`}
          </span>
        </div>
      )}
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
          {cutEntries.map((seg) => (
            <EditLogEntry
              key={seg.id}
              batchId={bid}
              segment={seg}
              kind="cut"
              isPending={pendingId === seg.id}
              exporting={exporting}
              isPreviewing={previewingId === seg.id}
              onTogglePreview={() => setPreviewingId(previewingId === seg.id ? null : seg.id)}
              onToggleStatus={() => handleToggle(seg)}
              onJumpToOutput={handleJumpToOutput}
              highlighted={highlightedId === seg.id}
            />
          ))}
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
            {keptEntries.map((seg) => (
              <EditLogEntry
                key={seg.id}
                batchId={bid}
                segment={seg}
                kind="kept"
                isPending={pendingId === seg.id}
                exporting={exporting}
                isPreviewing={previewingId === seg.id}
                onTogglePreview={() => setPreviewingId(previewingId === seg.id ? null : seg.id)}
                onToggleStatus={() => handleToggle(seg)}
                onJumpToOutput={handleJumpToOutput}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

interface EditLogEntryProps {
  batchId: string
  segment: Segment
  kind: 'cut' | 'kept'
  isPending: boolean
  exporting: boolean
  isPreviewing: boolean
  onTogglePreview: () => void
  onToggleStatus: () => void
  onJumpToOutput: (seconds: number) => void
  highlighted?: boolean
}

// Shared row for both the cut-review list and the kept-segments list --
// used to be two near-identical inline blocks; pulled out once thumbnail/
// duration/jump-to-output needed to land in both without drifting apart.
function EditLogEntry({
  batchId,
  segment: seg,
  kind,
  isPending,
  exporting,
  isPreviewing,
  onTogglePreview,
  onToggleStatus,
  onJumpToOutput,
  highlighted = false,
}: EditLogEntryProps) {
  const isHardCut = seg.origin === 'hard_cut'
  const restored = kind === 'cut' && seg.status === 'kept'
  const userRemoved = kind === 'kept' && seg.status === 'cut'
  const duration = seg.end_s - seg.start_s
  // Only present once a real export has run and this segment landed in
  // it (pipeline.manifest.apply_output_offsets) -- absent, not guessed,
  // for a segment currently excluded from the output (e.g. still cut),
  // same "position unknown" contract SkippableVideo's own skip-ahead
  // logic already follows.
  const canJump = seg.output_start_s != null

  const toggleLabel =
    kind === 'cut'
      ? isPending
        ? 'Saving...'
        : restored
          ? 'Cut again'
          : 'Restore'
      : isPending
        ? 'Saving...'
        : userRemoved
          ? 'Restore'
          : 'Cut'

  return (
    <li
      id={`edit-log-entry-${seg.id}`}
      className={`entry-card${isHardCut ? ' hard-cut' : ''}${restored ? ' restored' : ''}${userRemoved ? ' user-removed' : ''}${highlighted ? ' jump-highlight' : ''}`}
    >
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        <SegmentThumbnail batchId={batchId} segment={seg} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
            <div>
              <strong>
                {seg.start} - {seg.end}
              </strong>
              <span className="muted" style={{ marginLeft: 8 }}>({formatDuration(duration)})</span>
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
              {userRemoved && (
                <span className="badge badge-neutral" style={{ marginLeft: 10 }}>
                  Removed
                </span>
              )}
            </div>
            <div>
              {canJump && (
                <button
                  className="secondary"
                  type="button"
                  title="Jump to this point in the output player above"
                  onClick={() => onJumpToOutput(seg.output_start_s!)}
                  style={{ marginRight: 8 }}
                >
                  ↑ Jump to output
                </button>
              )}
              <button
                className="secondary"
                type="button"
                onClick={onTogglePreview}
                style={{ marginRight: 8 }}
              >
                {isPreviewing ? 'Hide preview' : 'Preview'}
              </button>
              <button type="button" onClick={onToggleStatus} disabled={isPending || exporting}>
                {toggleLabel}
              </button>
            </div>
          </div>
          {isPreviewing && (
            <SegmentPreview
              batchId={batchId}
              segment={seg}
              onToggleStatus={onToggleStatus}
              toggleDisabled={isPending || exporting}
              toggleLabel={toggleLabel}
            />
          )}
        </div>
      </div>
    </li>
  )
}

interface SegmentPreviewProps {
  batchId: string
  segment: Segment
  onToggleStatus: () => void
  toggleDisabled: boolean
  toggleLabel: string
}

// Scoped to the segment's own [start_s, end_s] window, not the source
// file's full timeline -- a native <video controls> here would show the
// whole source's scrubber (e.g. 3:10 for a 0.35s hard-cut window), which
// is exactly the gap this replaces (finding #1 of tonight's Edit Log
// usability audit). Custom play/pause + a scrubber whose range IS the
// segment's own duration, not the source file's.
function SegmentPreview({ batchId, segment, onToggleStatus, toggleDisabled, toggleLabel }: SegmentPreviewProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(false)
  // Seconds into the SEGMENT (0..duration), not the source file's own
  // currentTime -- what the scrubber and time readout are driven by.
  const [elapsed, setElapsed] = useState(0)
  const duration = Math.max(0, segment.end_s - segment.start_s)

  function handleLoadedMetadata(e: React.SyntheticEvent<HTMLVideoElement>) {
    e.currentTarget.currentTime = segment.start_s
  }

  function handleTimeUpdate(e: React.SyntheticEvent<HTMLVideoElement>) {
    const t = e.currentTarget.currentTime
    if (t >= segment.end_s) {
      e.currentTarget.pause()
    }
    setElapsed(Math.max(0, Math.min(duration, t - segment.start_s)))
  }

  function togglePlay() {
    const el = videoRef.current
    if (!el) return
    if (el.paused) {
      // Replay from the start once playback has reached the segment's
      // own end -- otherwise Play would do nothing (already-paused-at-end
      // is a dead click, not a real replay).
      if (el.currentTime < segment.start_s || el.currentTime >= segment.end_s) {
        el.currentTime = segment.start_s
      }
      void el.play()
    } else {
      el.pause()
    }
  }

  function handleScrubberClick(e: React.MouseEvent<HTMLDivElement>) {
    const el = videoRef.current
    if (!el || duration <= 0) return
    const rect = e.currentTarget.getBoundingClientRect()
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    el.currentTime = segment.start_s + frac * duration
  }

  // Keyboard shortcuts, scoped to while a preview is actually open (this
  // effect only runs while SegmentPreview is mounted, i.e. exactly one
  // entry's preview at a time -- previewingId in the parent guarantees
  // that). Space play/pauses; Enter runs the same restore/cut action the
  // toggle button does, for a reviewer doing many small fixes in a row
  // without reaching for the mouse each time. preventDefault on both is
  // real, not decorative: without it, Space would ALSO click whatever
  // button currently has focus (most likely the "Preview"/"Hide preview"
  // button the reviewer just clicked to get here), instantly closing the
  // preview they just opened -- a real collision found and fixed while
  // building this, not a hypothetical.
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null
      const tag = target?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target?.isContentEditable) {
        return
      }
      if (e.code === 'Space') {
        e.preventDefault()
        togglePlay()
      } else if (e.code === 'Enter') {
        e.preventDefault()
        if (!toggleDisabled) onToggleStatus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  })

  const frac = duration > 0 ? elapsed / duration : 0

  return (
    <div style={{ marginTop: 10 }}>
      <video
        ref={videoRef}
        src={sourceUrl(batchId, segment.source_file)}
        onLoadedMetadata={handleLoadedMetadata}
        onTimeUpdate={handleTimeUpdate}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        style={{ maxWidth: '100%', width: '100%', background: '#000' }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6 }}>
        <button className="secondary" type="button" onClick={togglePlay}>
          {playing ? 'Pause' : 'Play'}
        </button>
        <div className="segment-preview-scrubber" onClick={handleScrubberClick}>
          <div className="segment-preview-scrubber-fill" style={{ width: `${frac * 100}%` }} />
        </div>
        <span className="muted" style={{ fontSize: 13, whiteSpace: 'nowrap' }}>
          {formatDuration(elapsed)} / {formatDuration(duration)}
        </span>
      </div>
      <p className="muted" style={{ fontSize: 12, marginTop: 4, marginBottom: 0 }}>
        Space: play/pause · Enter: {toggleLabel}
      </p>
    </div>
  )
}

const THUMB_W = 96
const THUMB_H = 54

interface SegmentThumbnailProps {
  batchId: string
  segment: Segment
}

// A real frame from the segment's own midpoint, captured client-side (a
// hidden <video> seeked to that instant, drawn to a canvas) -- no new
// backend endpoint, reusing the same source-file byte-range serving the
// preview player already relies on. Lazy via IntersectionObserver: a
// full-length game batch can carry hundreds of entries (e.g. 681 on a
// real full_game.mkv run), and decoding a frame for every one of them
// eagerly would mean hundreds of concurrent <video> loads at once --
// only entries actually scrolled into view ever get a real decode.
function SegmentThumbnail({ batchId, segment }: SegmentThumbnailProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)
  const [dataUrl, setDataUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setVisible(true)
          observer.disconnect()
        }
      },
      { rootMargin: '200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!visible || dataUrl || failed) return
    const video = document.createElement('video')
    video.src = sourceUrl(batchId, segment.source_file)
    video.muted = true
    video.preload = 'metadata'
    let cancelled = false

    function cleanup() {
      video.removeAttribute('src')
      video.load()
    }

    function onLoadedMetadata() {
      if (cancelled) return
      const mid = segment.start_s + (segment.end_s - segment.start_s) / 2
      // Clamp inside the actually-decodable range -- a segment sitting
      // right at the source's own tail could otherwise request a seek
      // past what video.duration reports.
      video.currentTime = Math.min(mid, Math.max(0, video.duration - 0.05))
    }

    function onSeeked() {
      if (cancelled) return
      const canvas = document.createElement('canvas')
      canvas.width = THUMB_W
      canvas.height = THUMB_H
      const ctx = canvas.getContext('2d')
      if (!ctx) {
        setFailed(true)
        cleanup()
        return
      }
      try {
        ctx.drawImage(video, 0, 0, THUMB_W, THUMB_H)
        setDataUrl(canvas.toDataURL('image/jpeg', 0.7))
      } catch {
        setFailed(true)
      }
      cleanup()
    }

    function onError() {
      if (cancelled) return
      setFailed(true)
    }

    video.addEventListener('loadedmetadata', onLoadedMetadata)
    video.addEventListener('seeked', onSeeked)
    video.addEventListener('error', onError)

    return () => {
      cancelled = true
      video.removeEventListener('loadedmetadata', onLoadedMetadata)
      video.removeEventListener('seeked', onSeeked)
      video.removeEventListener('error', onError)
      cleanup()
    }
  }, [visible, dataUrl, failed, batchId, segment])

  return (
    <div
      ref={containerRef}
      className="segment-thumb"
      style={{ width: THUMB_W, height: THUMB_H, flexShrink: 0 }}
      aria-hidden="true"
    >
      {dataUrl && <img src={dataUrl} alt="" width={THUMB_W} height={THUMB_H} />}
    </div>
  )
}
