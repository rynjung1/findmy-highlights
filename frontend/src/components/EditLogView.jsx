import { useEffect, useState } from 'react'
import { getManifest, sourceUrl, updateSegmentStatus } from '../api'

// Only segments with origin "gap" were ever cut by detection (see
// pipeline/manifest.py: origin is set once at build time and never
// changes, independent of status, which restore/un-restore does flip)
// -- so this is the exact, permanent "was this ever a cut candidate"
// marker the spec's "every segment that was cut" listing needs,
// regardless of whether it's since been restored.
function isEditLogEntry(seg) {
  return seg.origin === 'gap'
}

export default function EditLogView({ batchId }) {
  const [manifest, setManifest] = useState(null)
  const [loadState, setLoadState] = useState('loading') // loading | ready | not_ready | error
  const [error, setError] = useState(null)
  const [previewingId, setPreviewingId] = useState(null)
  const [pendingId, setPendingId] = useState(null)

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
      } catch (err) {
        if (cancelled) return
        setError(err.message)
        setLoadState('error')
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [batchId])

  async function handleToggle(seg) {
    const nextStatus = seg.status === 'cut' ? 'kept' : 'cut'
    // Fires the instant the click is registered, before any request
    // goes out -- lets a real click be told apart from a request that
    // fired but is just hard to spot in a busy Network tab.
    console.log(`[EditLog] toggle clicked: segment=${seg.id} status=${seg.status} -> requesting ${nextStatus}`)
    setPendingId(seg.id)
    setError(null)
    try {
      await updateSegmentStatus(batchId, seg.id, nextStatus)
      // Re-fetch the manifest fresh from the server rather than merging
      // the PATCH response into local state. Whatever was causing the
      // UI to not reflect a real, confirmed-correct server-side change,
      // this removes the whole "local state drifted from server truth"
      // bug class outright: what's rendered is always exactly what the
      // server just reported, not what this component locally believes
      // happened.
      const fresh = await getManifest(batchId)
      if (fresh) {
        setManifest(fresh)
      } else {
        setError('Segment was updated, but re-fetching the manifest afterward failed.')
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setPendingId(null)
    }
  }

  if (loadState === 'loading') {
    return <p>Loading...</p>
  }

  if (loadState === 'not_ready') {
    return (
      <div>
        <h2>Edit Log</h2>
        <p>
          No processed video yet for this session -- upload and process a
          video from the Home tab first.
        </p>
      </div>
    )
  }

  if (loadState === 'error') {
    return (
      <div>
        <h2>Edit Log</h2>
        <p style={{ color: 'red' }}>{error}</p>
      </div>
    )
  }

  const cutEntries = manifest.segments.filter(isEditLogEntry)

  return (
    <div>
      <h2>Edit Log</h2>
      {error && <p style={{ color: 'red' }}>{error}</p>}
      {cutEntries.length === 0 ? (
        <p>Detection didn't cut anything from this video -- nothing to review here.</p>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0 }}>
          {cutEntries.map((seg) => {
            const restored = seg.status === 'kept'
            return (
              <li
                key={seg.id}
                style={{
                  border: '1px solid #ddd',
                  borderLeft: restored ? '4px solid #2a7a2a' : '4px solid #999',
                  borderRadius: 6,
                  padding: 12,
                  marginBottom: 10,
                  background: restored ? '#f1f8f1' : '#fff',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div>
                    <strong>
                      {seg.start} - {seg.end}
                    </strong>
                    <span style={{ color: '#666', marginLeft: 10 }}>{seg.source_file}</span>
                    {restored && (
                      <span style={{ marginLeft: 10, color: '#2a7a2a', fontWeight: 'bold' }}>
                        Restored
                      </span>
                    )}
                  </div>
                  <div>
                    <button
                      className="secondary"
                      onClick={() => setPreviewingId(previewingId === seg.id ? null : seg.id)}
                      style={{ marginRight: 8 }}
                    >
                      {previewingId === seg.id ? 'Hide preview' : 'Preview'}
                    </button>
                    <button onClick={() => handleToggle(seg)} disabled={pendingId === seg.id}>
                      {pendingId === seg.id ? 'Saving...' : restored ? 'Cut again' : 'Restore'}
                    </button>
                  </div>
                </div>
                {previewingId === seg.id && (
                  <SegmentPreview batchId={batchId} segment={seg} />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function SegmentPreview({ batchId, segment }) {
  function handleLoadedMetadata(e) {
    e.target.currentTime = segment.start_s
  }

  function handleTimeUpdate(e) {
    if (e.target.currentTime >= segment.end_s) {
      e.target.pause()
    }
  }

  return (
    <video
      controls
      src={sourceUrl(batchId, segment.source_file)}
      onLoadedMetadata={handleLoadedMetadata}
      onTimeUpdate={handleTimeUpdate}
      style={{ maxWidth: '100%', background: '#000', marginTop: 10 }}
    />
  )
}
