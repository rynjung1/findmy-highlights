import { useMemo, useRef, useState } from 'react'

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
function computeSkipWindows(segments) {
  if (!segments) return []
  const windows = []
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
