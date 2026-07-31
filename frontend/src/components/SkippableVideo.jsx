import { useMemo, useRef, useState } from 'react'

// KNOWN BROKEN, callers currently don't pass `segments` because of this
// (see ResultStep.jsx/EditLogView.jsx) -- do not re-enable by passing
// segments again without fixing this first. The assumption below --
// that a kept segment's position in the rendered output is just the
// cumulative sum of every earlier kept segment's own nominal duration --
// is false whenever pipeline/stitch.py's merge_overlapping_spans merges
// adjacent kept segments (any real gap shorter than the source's GOP,
// which is the common case, not an edge case) or a span's start shifts
// via keyframe-snap. Confirmed on a real batch: computed windows diverged
// from the real rendered output by up to 17s, meaning the skip button
// could appear at the wrong moment and jump to the wrong point on click.
// See README Known limitations for the full writeup. The real fix is for
// the manifest to carry real per-span rendered offsets computed once at
// stitch time, not re-derived here from an assumption already proven
// false.
function computeSkipWindows(segments) {
  if (!segments) return []
  const kept = segments
    .filter((s) => s.status === 'kept')
    .sort((a, b) => a.source_file_index - b.source_file_index || a.start_s - b.start_s)
  let cursor = 0
  const windows = []
  for (const seg of kept) {
    const segDuration = seg.end_s - seg.start_s
    for (const sug of seg.skip_suggestions || []) {
      windows.push({
        start: cursor + (sug.start_s - seg.start_s),
        end: cursor + (sug.end_s - seg.start_s),
      })
    }
    cursor += segDuration
  }
  return windows
}

// A <video> with an optional manual "Skip ahead" button, built from the
// manifest's skip_suggestions (see pipeline/manifest.py). Purely a
// non-destructive UI affordance: nothing is removed from the video, the
// button only appears while the playhead is inside a suggested quiet
// stretch WITHIN the currently-playing kept segment, and it's the
// viewer's choice whether to use it -- this never jumps on its own.
export default function SkippableVideo({ src, segments, style, ...videoProps }) {
  const videoRef = useRef(null)
  const skipWindows = useMemo(() => computeSkipWindows(segments), [segments])
  const [activeSkip, setActiveSkip] = useState(null)

  function handleTimeUpdate(e) {
    const t = e.target.currentTime
    const hit = skipWindows.find((w) => t >= w.start && t < w.end)
    setActiveSkip(hit || null)
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
      {activeSkip && (
        <button className="skip-ahead-button" onClick={handleSkipClick}>
          <span className="skip-ahead-icon" aria-hidden="true">⏭</span>
          Skip ahead
          <span className="skip-ahead-duration">
            {Math.max(1, Math.round(activeSkip.end - activeSkip.start))}s quiet
          </span>
        </button>
      )}
    </div>
  )
}
