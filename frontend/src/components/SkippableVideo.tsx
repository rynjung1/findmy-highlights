import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'
import type { Segment } from '../types'

// Exposed to parents via ref so something outside this component (e.g.
// an Edit Log entry's "jump to this point" affordance) can move the
// player without reaching into its internal <video> DOM node directly.
export interface SkippableVideoHandle {
  seekTo: (seconds: number) => void
}

const TOAST_VISIBLE_MS = 2000
const TOAST_FADE_MS = 300

// A seek doesn't land bit-exactly on the requested time -- the browser
// snaps to the nearest available frame/sample, which can be a hair short
// of the target. Without this tolerance, the timeupdate right after
// landing can still read as "inside the window" and re-trigger the same
// seek before the previous one ever stabilizes to a playing state --
// confirmed live as an unbounded reseek storm (81 repeated seeks at one
// boundary, 42 at another) that eventually left the video paused with no
// user action able to explain it.
const SKIP_ARRIVAL_EPSILON_S = 0.05

interface SkipWindow {
  start: number
  end: number
}

interface Toast {
  key: number
  message: string
  leaving: boolean
}

// Each kept segment's REAL position in the current output --
// output_start_s/output_end_s -- is computed once at stitch time
// (pipeline.stitch.compute_output_offsets, from the actually-extracted
// span files) and written onto the manifest by
// pipeline.manifest.apply_output_offsets. This function no longer
// re-derives that position itself: an earlier version assumed a kept
// segment's rendered position was the cumulative sum of every earlier
// kept segment's own nominal duration, which is false whenever
// pipeline.stitch.merge_overlapping_spans merges adjacent kept segments
// (the common case, not an edge case) or a span's start shifts via
// keyframe-snap -- confirmed on a real batch to diverge from the real
// rendered output by up to 17s. See README's skip-ahead retraction
// writeup for the full account. A segment with no output_start_s (an
// old manifest, or one that hasn't been (re-)exported since this
// shipped) simply contributes no suggestions -- position unknown is
// treated as "nothing to offer," never guessed.
//
// A suggestion's own real position is a simple linear offset within its
// segment's already-correct output_start_s: stream-copy/re-encode both
// preserve frame timing (this project never does variable-speed
// playback), so real time flows 1:1 with source-local time within one
// segment's own footage.
function computeSkipWindows(segments: Segment[] | undefined): SkipWindow[] {
  if (!segments) return []
  const windows: SkipWindow[] = []
  for (const seg of segments) {
    if (seg.status !== 'kept' || seg.output_start_s == null) continue
    for (const sug of seg.skip_suggestions || []) {
      windows.push({
        start: seg.output_start_s + (sug.start_s - seg.start_s),
        end: seg.output_start_s + (sug.end_s - seg.start_s),
      })
    }
  }
  return windows
}

interface SkippableVideoProps extends React.VideoHTMLAttributes<HTMLVideoElement> {
  src: string
  segments?: Segment[]
}

// A <video> with a skip-ahead affordance for the manifest's
// skip_suggestions (see pipeline/manifest.py). Purely non-destructive
// either way: nothing is ever removed from the video or the export --
// export/download always contains the complete output regardless of
// this component's state. Two modes, toggled by the viewer:
//   - Auto-skip (default on): reaching a quiet window's start jumps
//     currentTime to its end automatically, with a brief toast
//     explaining what just happened, so an auto-jump never reads as a
//     stutter or a glitch. Scrubbing back into a window plays it again
//     (nothing was deleted) -- but note that while auto-skip stays on,
//     landing back inside a window jumps forward again, same as the
//     first time; turn auto-skip off first to linger inside one.
//   - Manual (the original behavior, kept for anyone who'd rather
//     decide per-window): a "Skip ahead" button appears while playback
//     is inside a window and the viewer clicks it themselves.
const SkippableVideo = forwardRef<SkippableVideoHandle, SkippableVideoProps>(function SkippableVideo(
  { src, segments, style, ...videoProps },
  ref,
) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const skipWindows = useMemo(() => computeSkipWindows(segments), [segments])
  const [autoSkip, setAutoSkip] = useState(true)
  const [activeSkip, setActiveSkip] = useState<SkipWindow | null>(null)
  const [toast, setToast] = useState<Toast | null>(null)
  const toastTimers = useRef<ReturnType<typeof setTimeout>[]>([])

  useImperativeHandle(ref, () => ({
    seekTo(seconds: number) {
      const el = videoRef.current
      if (!el) return
      el.currentTime = seconds
      // A jump-to-here click is a request to actually watch that
      // moment, not just move the playhead silently underneath a
      // paused frame -- matches what a viewer expects from clicking a
      // timestamp.
      void el.play()
    },
  }))

  useEffect(() => () => toastTimers.current.forEach(clearTimeout), [])

  function showToast(message: string) {
    toastTimers.current.forEach(clearTimeout)
    const key = Date.now()
    setToast({ key, message, leaving: false })
    toastTimers.current = [
      setTimeout(() => setToast((cur) => (cur?.key === key ? { ...cur, leaving: true } : cur)),
        TOAST_VISIBLE_MS),
      setTimeout(() => setToast((cur) => (cur?.key === key ? null : cur)),
        TOAST_VISIBLE_MS + TOAST_FADE_MS),
    ]
  }

  function handleTimeUpdate(e: React.SyntheticEvent<HTMLVideoElement>) {
    const t = e.currentTarget.currentTime
    // Boundary tolerance: a window whose end we've already (near-)arrived
    // at no longer counts as "inside" it, even if frame-snap left us a
    // hair short of the exact end value.
    const hit = skipWindows.find((w) => t >= w.start && t < w.end - SKIP_ARRIVAL_EPSILON_S)
    if (autoSkip) {
      if (hit && videoRef.current) {
        // Re-entrancy guard: don't reissue the same seek if we're already
        // at (or within tolerance of) its target -- stops handleTimeUpdate
        // from re-triggering an identical seek on every tick before the
        // previous one has stabilized.
        const alreadyAtTarget = Math.abs(videoRef.current.currentTime - hit.end) < SKIP_ARRIVAL_EPSILON_S
        if (!alreadyAtTarget) {
          const seconds = Math.max(1, Math.round(hit.end - hit.start))
          videoRef.current.currentTime = hit.end
          showToast(`Skipped ${seconds}s of quiet time`)
        }
      }
      setActiveSkip(null)
    } else {
      setActiveSkip(hit || null)
    }
    videoProps.onTimeUpdate?.(e)
  }

  function handleSkipClick() {
    if (activeSkip && videoRef.current) {
      videoRef.current.currentTime = activeSkip.end
      setActiveSkip(null)
    }
  }

  return (
    <div className="skippable-video-wrap">
      <video
        {...videoProps}
        ref={videoRef}
        controls
        src={src}
        onTimeUpdate={handleTimeUpdate}
        style={{ maxWidth: '100%', width: '100%', background: '#000', ...style }}
      />
      {!autoSkip && activeSkip && (
        <button className="skip-ahead-button" onClick={handleSkipClick}>
          <span className="skip-ahead-icon" aria-hidden="true">⏭</span>
          Skip ahead
          <span className="skip-ahead-duration">
            {Math.max(1, Math.round(activeSkip.end - activeSkip.start))}s quiet
          </span>
        </button>
      )}
      {toast && (
        <div key={toast.key} className={`auto-skip-toast${toast.leaving ? ' leaving' : ''}`}>
          <span className="skip-ahead-icon" aria-hidden="true">⏭</span> {toast.message}
        </div>
      )}
      {skipWindows.length > 0 && (
        <label className="auto-skip-toggle">
          <input
            type="checkbox"
            checked={autoSkip}
            onChange={(e) => {
              setAutoSkip(e.target.checked)
              setActiveSkip(null)
            }}
          />
          Auto-skip quiet stretches
        </label>
      )}
    </div>
  )
})

export default SkippableVideo
