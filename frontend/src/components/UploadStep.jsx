import { useRef, useState } from 'react'
import { uploadBatch } from '../api'

// Mirrors backend/app.py's ALLOWED_VIDEO_EXTENSIONS -- this is the fast
// client-side rejection (immediate, before any bytes leave the
// browser); the server-side check is the real enforcement, this is
// purely a UX improvement, never trusted as the actual gate.
const ACCEPTED_EXTENSIONS = ['.mp4', '.mov', '.avi', '.mkv', '.m4v']

function isVideoFile(file) {
  const name = file.name.toLowerCase()
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext))
}

export default function UploadStep({ onUploaded }) {
  const fileInputRef = useRef(null)
  const [files, setFiles] = useState([])
  const [error, setError] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)

  function pickFiles(fileList) {
    const picked = Array.from(fileList)
    if (!picked.length) return
    const bad = picked.filter((f) => !isVideoFile(f))
    if (bad.length) {
      setError(
        `Unsupported file type: ${bad.map((f) => f.name).join(', ')}. ` +
          `Allowed: ${ACCEPTED_EXTENSIONS.join(', ')}`,
      )
      return
    }
    setError(null)
    setFiles(picked)
  }

  async function handleUpload() {
    if (!files.length) return
    setUploading(true)
    setError(null)
    try {
      const { batch_id } = await uploadBatch(files)
      onUploaded(batch_id)
    } catch (err) {
      setError(err.message)
    } finally {
      setUploading(false)
    }
  }

  return (
    <div className="content-grid">
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Upload game recording(s)</h2>
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragOver(false)
            pickFiles(e.dataTransfer.files)
          }}
          className={`dropzone${dragOver ? ' drag-over' : ''}`}
        >
          <svg
            className="dropzone-icon"
            width="52"
            height="52"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            aria-hidden="true"
          >
            <path d="M12 16V4M12 4l-4 4M12 4l4 4" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <p style={{ margin: '0 0 18px', fontWeight: 500, fontSize: 16 }}>
            Drag and drop video file(s) here
          </p>
          <input
            ref={fileInputRef}
            type="file"
            accept="video/*"
            multiple
            className="visually-hidden"
            onChange={(e) => pickFiles(e.target.files)}
          />
          <button type="button" className="secondary" onClick={() => fileInputRef.current?.click()}>
            Browse files
          </button>
          {files.length > 0 && (
            <ul className="file-chip-list">
              {files.map((f) => (
                <li key={f.name} className="file-chip">
                  {f.name} <span className="muted">({(f.size / 1024 / 1024).toFixed(1)} MB)</span>
                </li>
              ))}
            </ul>
          )}
          <p className="dropzone-hint">
            Supports {ACCEPTED_EXTENSIONS.join(', ')}
          </p>
        </div>
        {error && <p className="alert alert-danger">{error}</p>}
        <p>
          <button onClick={handleUpload} disabled={!files.length || uploading}>
            {uploading ? 'Uploading...' : `Upload ${files.length || ''} file(s)`}
          </button>
        </p>
      </div>

      <div className="tip-panel">
        <h3>Before you start</h3>
        <ul>
          <li>
            <strong>Multiple files?</strong> Upload every recording from the
            same game at once — they're stitched together in order
            automatically.
          </li>
          <li>
            <strong>Can't tell the order?</strong> If filenames don't make it
            obvious, you'll be asked to confirm the order before processing.
          </li>
          <li>
            <strong>Next up:</strong> you'll click home plate on a preview
            frame so detection knows where to look.
          </li>
        </ul>
      </div>
    </div>
  )
}
