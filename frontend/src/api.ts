// Thin wrapper around the backend's HTTP API (see backend/app.py) --
// every function here maps to exactly one endpoint, no client-side
// business logic.
//
// Backend base URL: empty string by default, meaning every path here
// stays relative -- which is what makes local dev work with zero CORS
// handling (Vite's dev-server proxy, see vite.config.ts, forwards
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
import type {
  BaseName,
  Calibration,
  DemoRunResponse,
  Job,
  JobType,
  Manifest,
  ReviewNextResponse,
  ReviewLabel,
  SegmentStatus,
  UploadResponse,
} from './types'

const API_BASE: string = import.meta.env.VITE_API_BASE_URL || ''

function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

async function request(path: string, options: RequestInit = {}): Promise<Response> {
  const res = await fetch(apiUrl(path), options)
  if (!res.ok) {
    let detail: string | undefined
    try {
      detail = (await res.json()).detail
    } catch {
      detail = res.statusText
    }
    throw new Error(detail || `HTTP ${res.status}`)
  }
  return res
}

export async function uploadBatch(files: File[]): Promise<UploadResponse> {
  const form = new FormData()
  for (const f of files) form.append('files', f)
  const res = await request('/batches', { method: 'POST', body: form })
  return res.json()
}

// Runs the bundled sample clip through the real pipeline end to end --
// no upload, no calibration step (see backend/demo.py). Returns the same
// shape as triggerProcess plus batch_id, so the caller can jump straight
// to the 'processing' stage exactly as if calibration had just finished.
export async function runDemo(): Promise<DemoRunResponse> {
  const res = await request('/demo/run', { method: 'POST' })
  return res.json()
}

export async function getCalibration(batchId: string): Promise<Calibration | null> {
  const res = await fetch(apiUrl(`/batches/${batchId}/calibration`))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export interface CalibrationPoint {
  x: number
  y: number
}

// `bases`, if given, is independently optional per base name (a camera
// angle that only shows first base submits only that one) -- mirrors
// backend/app.py's own <name>_x/<name>_y form fields exactly, one per
// marked base, so an unmarked base simply never appears in the form
// body rather than being sent as null/0.
export async function setCalibrationCoords(
  batchId: string,
  plate: CalibrationPoint,
  bases?: Partial<Record<BaseName, CalibrationPoint>>,
): Promise<Calibration> {
  const form = new FormData()
  form.append('x', String(plate.x))
  form.append('y', String(plate.y))
  if (bases) {
    for (const [name, point] of Object.entries(bases)) {
      if (!point) continue
      form.append(`${name}_x`, String(point.x))
      form.append(`${name}_y`, String(point.y))
    }
  }
  const res = await request(`/batches/${batchId}/calibration`, {
    method: 'POST',
    body: form,
  })
  return res.json()
}

export function previewUrl(batchId: string): string {
  // cache-bust: a retry after an error shouldn't ever show a stale
  // cached preview from a previous attempt
  return apiUrl(`/batches/${batchId}/preview.jpg?_=${Date.now()}`)
}

export interface TriggerProcessOptions {
  order?: string[]
  allow_uncalibrated?: boolean
}

export async function triggerProcess(
  batchId: string,
  opts: TriggerProcessOptions = {},
): Promise<Job> {
  const res = await request(`/batches/${batchId}/process`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  })
  return res.json()
}

export async function confirmOrder(batchId: string, order: string[]): Promise<Job> {
  const res = await request(`/batches/${batchId}/order`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order }),
  })
  return res.json()
}

export async function getJob(batchId: string, jobType: JobType): Promise<Job | null> {
  const res = await fetch(apiUrl(`/batches/${batchId}/jobs/${jobType}`))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function getManifest(batchId: string): Promise<Manifest | null> {
  const res = await fetch(apiUrl(`/batches/${batchId}/manifest`))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export function outputUrl(batchId: string, cacheBust?: string | number): string {
  // cacheBust is optional so ResultStep's existing usage (one output
  // per session, no reason to distrust the cache) is unaffected; the
  // Edit Log passes a fresh value after every re-export so the browser
  // can't serve back a stale video from before the restore took effect.
  return apiUrl(
    cacheBust ? `/batches/${batchId}/output?_=${cacheBust}` : `/batches/${batchId}/output`,
  )
}

export function sourceUrl(batchId: string, filename: string): string {
  return apiUrl(`/batches/${batchId}/source/${encodeURIComponent(filename)}`)
}

export async function updateSegmentStatus(
  batchId: string,
  segmentId: string,
  status: SegmentStatus,
) {
  const res = await request(`/batches/${batchId}/manifest/segments/${segmentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  })
  return res.json()
}

export async function triggerExport(batchId: string): Promise<Job> {
  const res = await request(`/batches/${batchId}/export`, { method: 'POST' })
  return res.json()
}

// Tier 1 review queue (see backend/app.py, pipeline/review.py) -- a
// global queue, not scoped to any one batch, so these don't take a
// batchId. A 404 from getNextReview means the server has no
// training_data_dir configured at all (FMH_TRAINING_DATA_DIR unset),
// which ReviewQueueView treats as a distinct "not enabled" state from
// an empty-but-enabled queue ({done: true}, a normal 200).
export async function getNextReview(): Promise<ReviewNextResponse | null> {
  const res = await fetch(apiUrl('/review/next'))
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function labelReview(
  reviewId: string,
  label: ReviewLabel,
  note?: string,
): Promise<ReviewNextResponse> {
  const res = await request(`/review/${reviewId}/label`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, note: note || null }),
  })
  return res.json()
}

export function reviewClipUrl(reviewId: string): string {
  return apiUrl(`/review/${reviewId}/clip`)
}
