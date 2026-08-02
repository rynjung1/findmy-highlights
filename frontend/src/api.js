// Thin wrapper around the backend's HTTP API (see backend/app.py) --
// every function here maps to exactly one endpoint, no client-side
// business logic. Proxied through Vite's dev server (see vite.config.js)
// so all paths are relative.

async function request(path, options = {}) {
  const res = await fetch(path, options)
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

export async function getCalibration(batchId) {
  const res = await fetch(`/batches/${batchId}/calibration`)
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
  return `/batches/${batchId}/preview.jpg?_=${Date.now()}`
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
  const res = await fetch(`/batches/${batchId}/jobs/${jobType}`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function getManifest(batchId) {
  const res = await fetch(`/batches/${batchId}/manifest`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export function outputUrl(batchId, cacheBust) {
  // cacheBust is optional so ResultStep's existing usage (one output
  // per session, no reason to distrust the cache) is unaffected; the
  // Edit Log passes a fresh value after every re-export so the browser
  // can't serve back a stale video from before the restore took effect.
  return cacheBust ? `/batches/${batchId}/output?_=${cacheBust}` : `/batches/${batchId}/output`
}

export function sourceUrl(batchId, filename) {
  return `/batches/${batchId}/source/${encodeURIComponent(filename)}`
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
  const res = await fetch('/review/next')
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
  return `/review/${reviewId}/clip`
}
