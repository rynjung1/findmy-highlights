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

// Real operational failures this app has actually hit (see
// docs/INVESTIGATION_LOG.md): a transient backend-unreachable moment and
// a mid-write server disk-full error. Neither used to get any distinct
// handling -- every catch block across every component just showed
// whatever raw string ended up in err.message, which for a network
// failure is a vague, browser-specific engine string ("Failed to
// fetch", "Load failed", "NetworkError...") and for the old generic
// disk-full response was literally "Internal Server Error", both
// meaningless to a non-technical user. Classifying centrally here means
// every existing `err instanceof Error ? err.message : String(err)`
// call site across the app (there are many, all written the same way)
// gets the improved message for free, with no per-component changes.
export type AppErrorKind = 'network' | 'disk_full' | 'server'

export class AppError extends Error {
  kind: AppErrorKind
  constructor(message: string, kind: AppErrorKind) {
    super(message)
    this.name = 'AppError'
    this.kind = kind
  }
}

// Exported so callers reading a raw error string that never went through
// an HTTP error response can still get the same classification -- the
// one real case that matters: a background job (detect/export) that hit
// ENOSPC mid-run reports it via `job.error`, Python's own OSError string
// ("[Errno 28] No space left on device: '...'"), inside an otherwise-200
// `GET .../jobs/{type}` response body. That never passes through
// `request()`'s error branch at all (the HTTP request itself succeeded;
// it's the job that failed), so ProcessingStep/EditLogView need to run
// job.error through this themselves before displaying it.
export function classifyMessage(raw: string): { message: string; kind: AppErrorKind } {
  const lower = raw.toLowerCase()
  if (lower.includes('no space left on device') || lower.includes('enospc')) {
    return {
      message:
        'The server ran out of disk space while handling this. Free up ' +
        'space on the server, then try again.',
      kind: 'disk_full',
    }
  }
  return { message: raw, kind: 'server' }
}

const UNREACHABLE_MESSAGE =
  "Can't reach the Find My Highlights server right now. Make sure it's " +
  'running, then try again.'

function throwClassified(raw: string): never {
  const { message, kind } = classifyMessage(raw)
  throw new AppError(message, kind)
}

// Every request in this module funnels through here or handleErrorResponse
// below -- the one place that turns "fetch() itself rejected" into a
// real, classified, friendly error instead of letting each call site
// rediscover the same raw TypeError independently.
async function fetchOrThrow(path: string, options?: RequestInit): Promise<Response> {
  try {
    return await fetch(apiUrl(path), options)
  } catch {
    // fetch() rejects (not a 4xx/5xx response -- an actual rejection)
    // when the request never reached ANY server at all: offline, DNS
    // failure, or a direct connection refused with no proxy in front
    // (the real topology a production deploy with VITE_API_BASE_URL
    // pointed straight at the backend would have). Every browser
    // signals this the same way, with wording that means nothing to
    // someone who isn't a developer.
    throw new AppError(UNREACHABLE_MESSAGE, 'network')
  }
}

// Every non-ok response funnels through here. Real, live-tested finding
// (not assumed): in this project's actual local-dev topology, killing
// the backend process does NOT make fetch() reject at all -- Vite's own
// dev proxy (see vite.config.ts) catches the real ECONNREFUSED
// server-side and hands the browser a normal-looking HTTP 500 with no
// body ("[vite] http proxy error: ... ECONNREFUSED" in the vite log,
// confirmed live). A production deploy behind a reverse proxy in front
// of a crashed backend looks the same way for the same reason. This
// app's own FastAPI backend, by contrast, ALWAYS returns a real JSON
// {"detail": ...} body on a real handled error (every HTTPException
// here does) -- so a non-ok response with no parseable JSON detail is
// itself the signal: not a real answer from this app, an
// infrastructure-level failure between the browser and it.
async function handleErrorResponse(res: Response): Promise<never> {
  let detail: string | undefined
  try {
    detail = (await res.json()).detail
  } catch {
    throw new AppError(UNREACHABLE_MESSAGE, 'network')
  }
  throwClassified(detail || `HTTP ${res.status}`)
}

async function request(path: string, options: RequestInit = {}): Promise<Response> {
  const res = await fetchOrThrow(path, options)
  if (!res.ok) await handleErrorResponse(res)
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
  const res = await fetchOrThrow(`/batches/${batchId}/calibration`)
  if (res.status === 404) return null
  if (!res.ok) await handleErrorResponse(res)
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

// Re-uploads an already-fetched Calibration (e.g. from a previous batch
// in the same session) onto a new batch, via the same calibration_file
// path scripts/calibrate.py's non-interactive mode and the multipart
// upload branch of POST /batches/{id}/calibration already support --
// no new backend endpoint needed. Built for the multi-game queue's
// "reuse the previous game's calibration" fast path (same physical
// camera setup, several games in one session): round-trips the exact
// JSON the server already validated once, so it satisfies the same
// plate_xy/zone_radius_px checks trivially.
export async function setCalibrationFile(
  batchId: string,
  calibration: Calibration,
): Promise<Calibration> {
  const form = new FormData()
  const blob = new Blob([JSON.stringify(calibration)], { type: 'application/json' })
  form.append('calibration_file', blob, 'calibration.json')
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
  const res = await fetchOrThrow(`/batches/${batchId}/jobs/${jobType}`)
  if (res.status === 404) return null
  if (!res.ok) await handleErrorResponse(res)
  return res.json()
}

export async function getManifest(batchId: string): Promise<Manifest | null> {
  const res = await fetchOrThrow(`/batches/${batchId}/manifest`)
  if (res.status === 404) return null
  if (!res.ok) await handleErrorResponse(res)
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
  const res = await fetchOrThrow('/review/next')
  if (res.status === 404) return null
  if (!res.ok) await handleErrorResponse(res)
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
