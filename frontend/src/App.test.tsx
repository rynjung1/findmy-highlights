import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import * as api from './api'
import type { Calibration, Job } from './types'

// Real, component-level tests of the multi-game queue's outer loop
// (App.tsx's startNextQueuedItem and its wiring into ProcessingStep's
// onDone/onError) -- per the task's own allowance, no full browser
// simulation (that's already covered live, see tonight's real
// Playwright run against a running backend). The api module is mocked
// entirely so sequencing/reuse/failure-handling can be driven and
// asserted deterministically.
//
// Demo mode (runDemo) is used for "item 1" throughout, deliberately:
// it skips CalibrateStep's own image-click interaction (unrelated,
// pre-existing, already-shipped logic this suite isn't testing),
// letting these tests focus purely on the queue's own new behavior --
// advancement, calibration reuse, the opt-out toggle, and failure
// recovery.
//
// Item 1's detect job is deliberately GATED (an unresolved Promise held
// open until the test explicitly resolves it) rather than resolved
// immediately -- item 1's real completion can otherwise race ahead of
// "add game 2 to the queue" within the same test (both are real async
// chains with no natural ordering guarantee), which would let item 1
// finish before item 2 even exists to advance into -- a real flake this
// suite hit and fixed by removing the race, not by hoping the timing
// works out.

function completedJob(overrides: Partial<Job> = {}): Job {
  return {
    job_id: 'j', batch_id: 'x', type: 'detect', status: 'completed', stage: null,
    started_at: '', updated_at: '', suggested_order: null, order_reason: null,
    warnings: [], error: null, manifest_path: null, output_path: null,
    ...overrides,
  }
}

function fakeCalibration(): Calibration {
  return { frame_size: [1920, 1080], plate_xy: [1147, 840], zone_radius_px: 280, created_from: 'demo' }
}

/** A getJob mock where EVERY call for `gatedBatchId`'s detect job hangs
 * until `release(job)` is called; every other call (including later
 * ones for the same batch/job type, or for other batches) resolves
 * immediately to a completed job. Deliberately re-entrant rather than a
 * one-shot promise: onDone/onError are recreated on every App render
 * (pre-existing App.tsx pattern), which restarts ProcessingStep's own
 * poll() effect (its deps array includes them) any time this test's own
 * actions (e.g. adding game 2) cause a re-render -- a one-shot gate
 * would only hold the FIRST, soon-orphaned poll() call, while a fresh
 * one immediately following it would sail through ungated. Every call,
 * from any effect instance, must wait for the same real release. */
function gatedGetJob(gatedBatchId: string) {
  let released = false
  let releasedJob: Job | null = null
  const waiters: Array<(job: Job) => void> = []
  const release = (job: Job) => {
    released = true
    releasedJob = job
    waiters.splice(0).forEach((resolve) => resolve(job))
  }
  const impl = vi.fn((batchId: string, jobType: 'detect' | 'export'): Promise<Job> => {
    if (batchId === gatedBatchId && jobType === 'detect') {
      if (released) return Promise.resolve(releasedJob!)
      return new Promise<Job>((resolve) => waiters.push(resolve))
    }
    return Promise.resolve(completedJob({ batch_id: batchId, type: jobType }))
  })
  return { impl, release }
}

async function addSecondGame(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '+ Add another game' }))
  const fileInput = document.querySelector('.card:has(> h2) input[type=file]') as HTMLInputElement
  await user.upload(fileInput, new File(['x'], 'clip2.mkv', { type: 'video/x-matroska' }))
  await user.click(await screen.findByRole('button', { name: /^Upload 1 file/ }))
  await waitFor(() => expect(screen.getByText('Game 2')).toBeInTheDocument())
}

describe('App.tsx multi-game queue', () => {
  // Separate counters (not one shared one) so a real upload's batch id
  // is always "b1" for the first one regardless of whether a demo run
  // happened first -- keeps every assertion below readable as exactly
  // the id it names, rather than an off-by-one from an unrelated mock.
  let demoCounter: number
  let uploadCounter: number

  beforeEach(() => {
    localStorage.clear()
    demoCounter = 0
    uploadCounter = 0
    vi.spyOn(api, 'runDemo').mockImplementation(async () => {
      demoCounter += 1
      return { ...completedJob(), batch_id: `demo${demoCounter}`, status: 'pending' }
    })
    vi.spyOn(api, 'uploadBatch').mockImplementation(async () => {
      uploadCounter += 1
      return { batch_id: `b${uploadCounter}`, files: ['clip.mkv'] }
    })
    vi.spyOn(api, 'getCalibration').mockResolvedValue(fakeCalibration())
    vi.spyOn(api, 'setCalibrationFile').mockResolvedValue(fakeCalibration())
    vi.spyOn(api, 'triggerProcess').mockResolvedValue(completedJob({ status: 'pending' }))
    vi.spyOn(api, 'getManifest').mockResolvedValue(null)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('advances to a queued second item automatically once the first finishes, reusing its calibration with no manual click', async () => {
    const { impl, release } = gatedGetJob('demo1')
    vi.spyOn(api, 'getJob').mockImplementation(impl)

    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('button', { name: 'Try the demo' }))
    await waitFor(() => expect(screen.getByText('Demo')).toBeInTheDocument())

    await addSecondGame(user)
    expect(screen.getByText('Queued')).toBeInTheDocument()

    // Only now let item 1 actually finish.
    release(completedJob({ batch_id: 'demo1', type: 'detect', status: 'completed' }))

    await waitFor(() => expect(api.getCalibration).toHaveBeenCalledWith('demo1'))
    await waitFor(() => expect(api.setCalibrationFile).toHaveBeenCalledWith('b1', fakeCalibration()))
    await waitFor(() => expect(api.triggerProcess).toHaveBeenCalledWith('b1'))

    // Scoped to the queue sidebar specifically -- "Done" also appears
    // in the step tracker's own last step label and in ResultStep's own
    // badge, neither of which this assertion is about.
    await waitFor(() => {
      const queueList = document.querySelector('.queue-list') as HTMLElement
      const doneBadges = within(queueList).getAllByText('Done')
      expect(doneBadges.length).toBe(2)
    })
  })

  it('does not call getCalibration/setCalibrationFile for an item marked "Reuse calibration -> Will recalibrate"', async () => {
    const { impl, release } = gatedGetJob('demo1')
    vi.spyOn(api, 'getJob').mockImplementation(impl)

    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('button', { name: 'Try the demo' }))
    await addSecondGame(user)

    // Real opt-out toggle, on the still-queued item.
    await user.click(screen.getByRole('button', { name: 'Reuse calibration' }))
    expect(screen.getByRole('button', { name: 'Will recalibrate' })).toBeInTheDocument()

    release(completedJob({ batch_id: 'demo1', type: 'detect', status: 'completed' }))

    // Item 2 becomes active and lands on the normal manual calibrate
    // screen instead of auto-processing.
    await waitFor(() => expect(screen.getByText('Mark home plate', { exact: false })).toBeInTheDocument())

    expect(api.getCalibration).not.toHaveBeenCalledWith('b1')
    expect(api.setCalibrationFile).not.toHaveBeenCalled()
  })

  it('continues to the next queued item when the current one FAILS, rather than hanging', async () => {
    const { impl, release } = gatedGetJob('demo1')
    vi.spyOn(api, 'getJob').mockImplementation(impl)

    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('button', { name: 'Try the demo' }))
    await addSecondGame(user)

    // Only now let item 1 actually FAIL.
    release(
      completedJob({
        batch_id: 'demo1',
        type: 'detect',
        status: 'failed',
        error: 'could not open video: corrupt.mkv',
      }),
    )

    await waitFor(() => expect(screen.getByText('Something went wrong')).toBeInTheDocument())
    expect(screen.getByText('Failed')).toBeInTheDocument() // queue sidebar reflects it

    // Item 2 still gets triggered automatically -- not left hanging
    // behind the global single-job-at-a-time lock, which a real failure
    // has already released server-side (see backend/jobs.py's
    // RUNNING_STATUSES, excluding "failed").
    await waitFor(() => expect(api.getCalibration).toHaveBeenCalledWith('demo1'))
    await waitFor(() => expect(api.triggerProcess).toHaveBeenCalledWith('b1'))
    await waitFor(() => expect(screen.getAllByText('Done').length).toBeGreaterThanOrEqual(1))
  })
})
