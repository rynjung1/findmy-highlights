import { outputUrl } from '../api'
import SkippableVideo from './SkippableVideo'

export default function ResultStep({ batchId }) {
  const url = outputUrl(batchId)

  return (
    <div className="card">
      <span className="badge badge-success" style={{ marginBottom: 10 }}>Done</span>
      <h2 style={{ marginTop: 0 }}>Your highlight video is ready</h2>
      {/* range-request support on the backend (FileResponse) lets this
          player seek without downloading the whole file first */}
      {/* Skip-ahead suggestions temporarily disabled (not passing segments):
          computeSkipWindows in SkippableVideo.jsx assumes a kept manifest
          segment's position in the rendered output is the cumulative sum
          of prior kept segments' nominal durations, but real stitching can
          merge adjacent segments or shift a span's start via keyframe-snap
          -- confirmed on a real batch to diverge from the actual rendered
          position by up to 17s, meaning the button can appear at the wrong
          moment and jump to the wrong point on click. See README Known
          limitations for the full writeup. Re-enable once SkippableVideo
          consumes real per-span offsets from the manifest instead of
          re-deriving them client-side. */}
      <SkippableVideo src={url} />
      <p>
        <a href={url} download="highlights.mp4">
          <button>Download</button>
        </a>
      </p>
    </div>
  )
}
