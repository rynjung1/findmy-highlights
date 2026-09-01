import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ProcessingStep from './ProcessingStep'
import * as api from '../api'
import type { Job } from '../types'

// parseStage() itself is a private, unexported function in
// ProcessingStep.tsx -- these are real, component-level tests (per the
// task's own allowance), exercising the exact same parsing through the
// component's rendered output, against the REAL stage-string shapes the
// backend actually sends. Confirmed against backend/pipeline_runner.py's
// on_stage wrapping (`f"{stage} ({Path(path).name})"`) and
// pipeline/run.py's own `f"analyzing motion ({t:.0f}s/{duration:.0f}s)"`
// -- a detect-stage string in production carries BOTH a progress group
// and a trailing filename group, e.g.
// "analyzing motion (13s/27s) (clip_base3.mkv)", not the simplified
// single-group form it'd be easy to test in isolation instead.

function baseJob(overrides: Partial<Job> = {}): Job {
  return {
    job_id: 'j1',
    batch_id: 'b1',
    type: 'detect',
    status: 'in_progress',
    stage: null,
    started_at: '',
    updated_at: '',
    suggested_order: null,
    order_reason: null,
    warnings: [],
    error: null,
    manifest_path: null,
    output_path: null,
    ...overrides,
  }
}

async function renderWithStage(stage: string | null) {
  vi.spyOn(api, 'getJob').mockImplementation(async (_batchId, jobType) => {
    if (jobType === 'detect') return baseJob({ stage })
    return null // export job not created yet -- keeps the poll loop from advancing past what we're inspecting
  })
  render(<ProcessingStep batchId="b1" onDone={() => {}} onError={() => {}} />)
  // Each caller runs its own real waitFor() with a meaningful assertion
  // right after -- this helper's job is only to render and mock, not to
  // pre-judge what "loaded" looks like for every stage string it's used
  // with.
}

describe('ProcessingStep stage parsing (parseStage, exercised via render)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows "starting..." with no percent when stage is null', async () => {
    await renderWithStage(null)
    await waitFor(() => expect(screen.getByText(/starting\.\.\./)).toBeInTheDocument())
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('parses a real motion-analysis stage string with embedded progress and a filename suffix', async () => {
    await renderWithStage('analyzing motion (13s/27s) (clip_base3.mkv)')
    await waitFor(() => expect(screen.getByText(/Analyzing motion/)).toBeInTheDocument())
    // 13/27 = 48.1...% -> rounds to 48
    expect(screen.getByText(/48% through the video/)).toBeInTheDocument()
  })

  it('parses a real motion-analysis stage string at 0% (job just started)', async () => {
    await renderWithStage('analyzing motion (0s/27s) (clip_base3.mkv)')
    await waitFor(() => expect(screen.getByText(/Analyzing motion/)).toBeInTheDocument())
    expect(screen.getByText(/0% through the video/)).toBeInTheDocument()
  })

  it('maps "running player detection" to its human label, with no percent before progress appears', async () => {
    await renderWithStage('running player detection (clip_base3.mkv)')
    await waitFor(() => expect(screen.getByText(/Detecting plays/)).toBeInTheDocument())
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('parses real detection progress once it appears', async () => {
    await renderWithStage('running player detection (3s/30s) (clip_base3.mkv)')
    await waitFor(() => expect(screen.getByText(/Detecting plays/)).toBeInTheDocument())
    // 3/30 = 10%
    expect(screen.getByText(/10% through the video/)).toBeInTheDocument()
  })

  it('maps every other known stage to its human label with no percent (no per-frame signal for these)', async () => {
    const cases: [string, string][] = [
      ['extending and padding segments (clip_base3.mkv)', 'Refining play boundaries'],
      ['building manifest', 'Finalizing analysis'],
      ['extracting kept segments', 'Cutting downtime'],
      ['stitching output', 'Assembling final video'],
    ]
    for (const [raw, label] of cases) {
      await renderWithStage(raw)
      await waitFor(() => expect(screen.getByText(label)).toBeInTheDocument())
      expect(screen.queryByText(/%/)).not.toBeInTheDocument()
      vi.restoreAllMocks()
    }
  })

  it('falls back to the raw stage text for an unrecognized future stage, rather than going blank', async () => {
    await renderWithStage('some future stage nobody mapped yet')
    await waitFor(() =>
      expect(screen.getByText('some future stage nobody mapped yet')).toBeInTheDocument(),
    )
  })

  it('does not compute a percent when total is 0 (division-by-zero guard)', async () => {
    await renderWithStage('analyzing motion (0s/0s) (empty.mkv)')
    await waitFor(() => expect(screen.getByText(/Analyzing motion/)).toBeInTheDocument())
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('clamps percent to 100 if done ever exceeds total', async () => {
    await renderWithStage('analyzing motion (31s/30s) (clip.mkv)')
    await waitFor(() => expect(screen.getByText(/100% through the video/)).toBeInTheDocument())
  })
})
