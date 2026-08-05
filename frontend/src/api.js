// Thin wrapper around the backend's HTTP API (see backend/app.py) --
// every function here maps to exactly one endpoint, no client-side
// business logic.
//
// Backend base URL: empty string by default, meaning every path here
// stays relative -- which is what makes local dev work with zero CORS
// handling (Vite's dev-server proxy, see vite.config.js, forwards
// relative /batches and /review paths to the backend on the same
// origin from the browser's point of view). A production deployment
// with the frontend on a different domain from the backend (e.g. a
// static host + a separately-hosted API) sets VITE_API_BASE_URL at
// BUILD time (Vite only substitutes import.meta.env.VITE_* at build
// time, not runtime -- see README's Deployment section) to the
// backend's real absolute URL, e.g.
// "https://findmy-highlights-api.example.com". The backend's own
// FMH_CORS_ORIGINS must then be set to the frontend's origin for these
// cross-origin requests to actually succeed.
const API_BASE = import.meta.env.VITE_API_BASE_URL || ''

function apiUrl(path) {
  return `${API_BASE}${path}`
}

async function request(path, options = {}) {
  const res = await fetch(apiUrl(path), options)
  if (!res.ok) {
    let detail
    try {
      detail = (await res.json()).detail
    } catch {
      detail = res.statusText
    }
    throw new Error(detail || `HTTP ${res.status}`)
  }
  return res
}

export async function uploadBatch(files) {
  const form = new FormData()
  for (const f of files) form.append('files', f)
  const res = await request('/batches', { method: 'POST', body: form })
  return res.json()
}

// Runs the bundled sample clip through the real pipeline end to end --
// no upload, no calibration step (see backend/demo.py). Returns the same
// shape as triggerProcess plus batch_id, so the caller can jump straight
// to the 'processing' stage exactly as if calibration had just finished.
export async function runDemo() {
  const res = await request('/demo/run', { method: 'POST' })
  return res.json()
}

export async function getCalibration(batchId) {
  const res = await fetch(apiUrl(`/batches/${batchId}/calibration`))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function setCalibrationCoords(batchId, x, y) {
  const form = new FormData()
  form.append('x', x)
  form.append('y', y)
  const res = await request(`/batches/${batchId}/calibration`, {
    method: 'POST',
    body: form,
  })
  return res.json()
}

export function previewUrl(batchId) {
  // cache-bust: a retry after an error shouldn't ever show a stale
  // cached preview from a previous attempt
  return apiUrl(`/batches/${batchId}/preview.jpg?_=${Date.now()}`)
}

export async function triggerProcess(batchId, opts = {}) {
  const res = await request(`/batches/${batchId}/process`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  })
  return res.json()
}

export async function confirmOrder(batchId, order) {
  const res = await request(`/batches/${batchId}/order`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order }),
  })
  return res.json()
}

export async function getJob(batchId, jobType) {
  const res = await fetch(apiUrl(`/batches/${batchId}/jobs/${jobType}`))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function getManifest(batchId) {
  const res = await fetch(apiUrl(`/batches/${batchId}/manifest`))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export function outputUrl(batchId, cacheBust) {
  // cacheBust is optional so ResultStep's existing usage (one output
  // per session, no reason to distrust the cache) is unaffected; the
  // Edit Log passes a fresh value after every re-export so the browser
  // can't serve back a stale video from before the restore took effect.
  return apiUrl(
    cacheBust ? `/batches/${batchId}/output?_=${cacheBust}` : `/batches/${batchId}/output`,
  )
}

export function sourceUrl(batchId, filename) {
  return apiUrl(`/batches/${batchId}/source/${encodeURIComponent(filename)}`)
}

export async function updateSegmentStatus(batchId, segmentId, status) {
  const res = await request(`/batches/${batchId}/manifest/segments/${segmentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  })
  return res.json()
}

export async function triggerExport(batchId) {
  const res = await request(`/batches/${batchId}/export`, { method: 'POST' })
  return res.json()
}

// Tier 1 review queue (see backend/app.py, pipeline/review.py) -- a
// global queue, not scoped to any one batch, so these don't take a
// batchId. A 404 from getNextReview means the server has no
// training_data_dir configured at all (FMH_TRAINING_DATA_DIR unset),
// which ReviewQueueView treats as a distinct "not enabled" state from
// an empty-but-enabled queue ({done: true}, a normal 200).
export async function getNextReview() {
  const res = await fetch(apiUrl('/review/next'))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function labelReview(reviewId, label, note) {
  const res = await request(`/review/${reviewId}/label`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, note: note || null }),
  })
  return res.json()
}

export function reviewClipUrl(reviewId) {
  return apiUrl(`/review/${reviewId}/clip`)
}
