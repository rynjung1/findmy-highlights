import { useEffect, useState } from 'react'
import { getManifest, outputUrl } from '../api'
import SkippableVideo from './SkippableVideo'

export default function ResultStep({ batchId }) {
  const url = outputUrl(batchId)
  const [manifest, setManifest] = useState(null)

  useEffect(() => {
    let cancelled = false
    // Fetched only for the manual skip-ahead affordance (see
    // SkippableVideo/pipeline.manifest's skip_suggestions) -- if this
    // fails for any reason the player still works, just without skip
    // suggestions, so no loading/error state is needed here.
    getManifest(batchId).then((m) => {
      if (!cancelled) setManifest(m)
    })
    return () => {
      cancelled = true
    }
  }, [batchId])

  return (
    <div className="card">
      <span className="badge badge-success" style={{ marginBottom: 10 }}>Done</span>
      <h2 style={{ marginTop: 0 }}>Your highlight video is ready</h2>
      {/* range-request support on the backend (FileResponse) lets this
          player seek without downloading the whole file first */}
      <SkippableVideo src={url} segments={manifest?.segments} />
      <p>
        <a href={url} download="highlights.mp4">
          <button>Download</button>
        </a>
      </p>
    </div>
  )
}
