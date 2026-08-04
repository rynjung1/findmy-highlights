import { useEffect, useState } from 'react'
import { getNextReview, labelReview, reviewClipUrl } from '../api'

const CANDIDATE_TYPE_LABEL = {
  hard_cut_dip: 'Hard cut',
  boundary_crossing: 'Segment boundary',
  control: 'Control sample',
}

// Tier 1 review queue (see README's Task 2 design, pipeline/review.py,
// backend/app.py): a global queue, not scoped to any one batch -- shows
// the pipeline's own most-borderline hard-cut and segment-boundary
// decisions, self-contained clip and all, and records a human's
// Downtime/Real-action verdict against them for later disagreement-rate
// reporting (scripts/review_stats.py). Nothing here changes any batch's
// manifest or output -- purely a labeling tool.
export default function ReviewQueueView() {
  // loading | disabled | empty | item | error
  const [loadState, setLoadState] = useState('loading')
  const [item, setItem] = useState(null)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [reviewedCount, setReviewedCount] = useState(0)
  const [remaining, setRemaining] = useState(null)

  useEffect(() => {
    loadNext()
  }, [])

  async function loadNext() {
    setLoadState('loading')
    setError(null)
    try {
      const next = await getNextReview()
      applyNext(next)
    } catch (err) {
      setError(err.message)
      setLoadState('error')
    }
  }

  function applyNext(next) {
    if (next === null) {
      setLoadState('disabled')
    } else if (next.done) {
      setItem(null)
      setRemaining(0)
      setLoadState('empty')
    } else {
      setItem(next)
      setRemaining(next.remaining)
      setLoadState('item')
    }
  }

  async function handleLabel(label) {
    if (!item) return
    setSubmitting(true)
    setError(null)
    try {
      const next = await labelReview(item.id, label)
      setReviewedCount((c) => c + 1)
      applyNext(next)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  if (loadState === 'loading') {
    return (
      <div className="card">
        <span className="spinner" aria-hidden="true" />
        Loading...
      </div>
    )
  }

  if (loadState === 'disabled') {
    return (
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Review Queue</h2>
        <p className="muted">
          The review queue isn't enabled on this server (the backend needs
          FMH_TRAINING_DATA_DIR set). Nothing to show here.
        </p>
      </div>
    )
  }

  if (loadState === 'error') {
    return (
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Review Queue</h2>
        <p className="alert alert-danger">{error}</p>
        <button className="secondary" onClick={loadNext}>Retry</button>
      </div>
    )
  }

  if (loadState === 'empty') {
    return (
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Review Queue</h2>
        <p className="muted">
          Nothing pending right now -- every borderline decision collected so
          far has been labeled.
          {reviewedCount > 0 && ` You labeled ${reviewedCount} this session.`}
        </p>
      </div>
    )
  }

  const isControl = item.candidate_type === 'control'

  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Review Queue</h2>
      <p className="muted">
        {reviewedCount > 0 ? `${reviewedCount} labeled this session. ` : ''}
        Watch the clip, then say whether the pipeline's decision was right.
      </p>
      {remaining !== null && (
        <p style={{ fontWeight: 600, margin: '4px 0 14px' }}>
          {remaining} remaining
        </p>
      )}

      {error && <p className="alert alert-danger">{error}</p>}

      <video
        key={item.id}
        controls
        autoPlay
        loop
        src={reviewClipUrl(item.id)}
        style={{ maxWidth: '100%', width: '100%', background: '#000', marginTop: 10 }}
      />

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, margin: '14px 0' }}>
        <span className="badge">{CANDIDATE_TYPE_LABEL[item.candidate_type] || item.candidate_type}</span>
        <span className="badge">
          pipeline decision: {item.pipeline_decision}
        </span>
        {item.margin !== null && item.margin !== undefined && (
          <span className="badge">margin: {item.margin.toFixed(4)}</span>
        )}
        {isControl && <span className="badge badge-warning">control sample</span>}
      </div>

      <p className="muted" style={{ fontSize: '0.9em' }}>
        {item.source.source_file} — {item.window.start_s.toFixed(2)}s
        {item.window.end_s !== item.window.start_s && ` to ${item.window.end_s.toFixed(2)}s`}
      </p>

      <div style={{ display: 'flex', gap: 10, marginTop: 16 }}>
        <button disabled={submitting} onClick={() => handleLabel('downtime')}>
          {submitting ? 'Saving...' : 'Downtime'}
        </button>
        <button disabled={submitting} onClick={() => handleLabel('real_action')}>
          {submitting ? 'Saving...' : 'Real action'}
        </button>
      </div>
    </div>
  )
}
