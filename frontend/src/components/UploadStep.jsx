import { useState } from 'react'
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
    <div>
      <h2>1. Upload game recording(s)</h2>
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
        style={{
          border: `2px dashed ${dragOver ? '#333' : '#aaa'}`,
          borderRadius: 8,
          padding: 40,
          textAlign: 'center',
          background: dragOver ? '#eee' : 'white',
        }}
      >
        <p>Drag and drop video file(s) here, or:</p>
        <input
          type="file"
          accept="video/*"
          multiple
          onChange={(e) => pickFiles(e.target.files)}
        />
        {files.length > 0 && (
          <ul style={{ textAlign: 'left', display: 'inline-block' }}>
            {files.map((f) => (
              <li key={f.name}>
                {f.name} ({(f.size / 1024 / 1024).toFixed(1)} MB)
              </li>
            ))}
          </ul>
        )}
      </div>
      {error && <p style={{ color: 'red' }}>{error}</p>}
      <p>
        <button onClick={handleUpload} disabled={!files.length || uploading}>
          {uploading ? 'Uploading...' : `Upload ${files.length || ''} file(s)`}
        </button>
      </p>
    </div>
  )
}
