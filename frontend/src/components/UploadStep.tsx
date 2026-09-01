import { useRef, useState } from 'react'
import { runDemo, uploadBatch } from '../api'

// Mirrors backend/app.py's ALLOWED_VIDEO_EXTENSIONS -- this is the fast
// client-side rejection (immediate, before any bytes leave the
// browser); the server-side check is the real enforcement, this is
// purely a UX improvement, never trusted as the actual gate.
const ACCEPTED_EXTENSIONS = ['.mp4', '.mov', '.avi', '.mkv', '.m4v']

function isVideoFile(file: File): boolean {
  const name = file.name.toLowerCase()
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext))
}

interface UploadStepProps {
  onUploaded: (batchId: string) => void
  onDemoStarted: (batchId: string) => void
  // Multi-game queue's "add another game" affordance reuses this exact
  // component (same drag-drop/validation/upload logic) rather than a
  // second copy -- `compact` just drops the demo card and the tips
  // panel, which only make sense for the very first upload, not for
  // adding game 2+ to an already-running queue. Defaults to false/
  // unset, so every existing render site (the first-upload screen) is
  // byte-for-byte unaffected.
  compact?: boolean
}

export default function UploadStep({ onUploaded, onDemoStarted, compact = false }: UploadStepProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [demoStarting, setDemoStarting] = useState(false)
  const [demoError, setDemoError] = useState<string | null>(null)

  async function handleTryDemo() {
    setDemoStarting(true)
    setDemoError(null)
    try {
      const { batch_id } = await runDemo()
      onDemoStarted(batch_id)
    } catch (err) {
      // The one real user-facing failure mode here is a 409 (another
      // job -- a real upload or someone else's demo run -- already in
      // progress; see backend/app.py's single-job-at-a-time rule), so
      // the message is shown as-is rather than a generic fallback.
      setDemoError(err instanceof Error ? err.message : String(err))
      setDemoStarting(false)
    }
  }

  function pickFiles(fileList: FileList) {
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
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUploading(false)
    }
  }

  return (
    <>
      {!compact && (
        <div className="card demo-card">
          <h2 style={{ marginTop: 0 }}>New here? Try it in one click</h2>
          <p className="muted">
            Runs a real ~45-second sample clip through the full pipeline --
            detection, highlight extraction, and export -- so you can see a
            finished result before uploading your own footage. Takes well
            under a minute.
          </p>
          {demoError && <p className="alert alert-danger">{demoError}</p>}
          <button onClick={handleTryDemo} disabled={demoStarting || uploading}>
            {demoStarting ? 'Starting demo...' : 'Try the demo'}
          </button>
        </div>
      )}

      <div className={compact ? undefined : 'content-grid'}>
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
            onChange={(e) => e.target.files && pickFiles(e.target.files)}
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

      {!compact && (
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
      )}
      </div>
    </>
  )
}
