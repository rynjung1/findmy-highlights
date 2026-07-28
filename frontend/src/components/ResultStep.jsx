import { outputUrl } from '../api'

export default function ResultStep({ batchId }) {
  const url = outputUrl(batchId)
  return (
    <div className="card">
      <span className="badge badge-success" style={{ marginBottom: 10 }}>Done</span>
      <h2 style={{ marginTop: 0 }}>Your highlight video is ready</h2>
      {/* range-request support on the backend (FileResponse) lets this
          player seek without downloading the whole file first */}
      <video controls src={url} style={{ maxWidth: '100%', width: '100%', background: '#000' }} />
      <p>
        <a href={url} download="highlights.mp4">
          <button>Download</button>
        </a>
      </p>
    </div>
  )
}
