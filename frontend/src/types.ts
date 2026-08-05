// Real shapes returned by backend/app.py's JSON endpoints -- kept here,
// not inline per-file, so every component agrees on what the server
// actually sends instead of each hand-rolling its own guess.

export type JobType = 'detect' | 'export'

// Mirrors backend/jobs.py's STATUSES exactly.
export type JobStatus =
  | 'pending'
  | 'in_progress'
  | 'needs_order_confirmation'
  | 'completed'
  | 'failed'
  | 'interrupted'

export interface Job {
  job_id: string
  batch_id: string
  type: JobType
  status: JobStatus
  stage: string | null
  started_at: string
  updated_at: string
  suggested_order: string[] | null
  order_reason: string | null
  warnings: string[]
  error: string | null
  manifest_path: string | null
  output_path: string | null
}

// backend.demo.seed_demo_batch's response: batch_id merged onto a
// freshly-created detect Job (see backend/app.py's POST /demo/run).
export type DemoRunResponse = Job & { batch_id: string }

export interface SkipSuggestion {
  start_s: number
  end_s: number
}

// Mirrors pipeline/manifest.py's real segment shape. `origin` is set
// once at build time and never changes (see EditLogView's own
// isEditLogEntry comment) -- these three are the only values the real
// pipeline produces.
export type SegmentOrigin = 'detected' | 'gap' | 'hard_cut'
export type SegmentStatus = 'kept' | 'cut'

export interface Segment {
  id: string
  source_file: string
  source_file_index: number
  start: string
  end: string
  start_s: number
  end_s: number
  detection_score: number
  status: SegmentStatus
  origin: SegmentOrigin
  skip_suggestions: SkipSuggestion[]
  // Only present once a real export has run (pipeline.manifest.
  // apply_output_offsets) -- absent on a manifest that's never been
  // stitched yet.
  output_start_s?: number
  output_end_s?: number
}

export interface Manifest {
  version: number
  source_files: string[]
  duration_s: number
  segments: Segment[]
}

export interface Calibration {
  frame_size: [number, number]
  plate_xy: [number, number]
  zone_radius_px: number
  created_from: string
}

export interface UploadResponse {
  batch_id: string
  files: string[]
}

// Mirrors backend/app.py's _review_response. `margin` is null for a
// control sample (see pipeline.review.review_priority_key).
export interface XClipFeatures {
  p_swinging: number
  pos_prompt: string
  neg_prompt: string
}

export interface ReviewRecord {
  id: string
  candidate_type: 'hard_cut_dip' | 'boundary_crossing' | 'control'
  pipeline_decision: string
  window: { start_s: number; end_s: number }
  margin: number | null
  features_at_label_time: {
    xclip?: XClipFeatures
    [key: string]: unknown
  }
  source: { source_file: string; [key: string]: unknown }
  created_at: string
  clip_url: string
  remaining: number
}

export type ReviewNextResponse = ReviewRecord | { done: true; remaining: number }

export type ReviewLabel = 'downtime' | 'real_action'
