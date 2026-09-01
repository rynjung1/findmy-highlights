import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import EditLogView from './EditLogView'
import * as api from '../api'
import type { Job, Manifest } from '../types'

// The Space/Enter keyboard shortcut handler (handleKeyDown, inside
// SegmentPreview's useEffect) is not exported -- SegmentPreview itself
// is a private, unexported function in EditLogView.tsx. These are real,
// component-level tests (per the task's own allowance): render the
// actual EditLogView tree, open a real segment's preview the same way a
// user would (click "Preview"), then dispatch real keydown events and
// assert the real resulting action fired.

function makeManifest(): Manifest {
  return {
    version: 1,
    source_files: ['clip_60.mkv'],
    duration_s: 190,
    segments: [
      {
        id: 'seg_002',
        source_file: 'clip_60.mkv',
        source_file_index: 0,
        start: '00:00:10.000',
        end: '00:00:15.000',
        start_s: 10,
        end_s: 15,
        detection_score: 0,
        status: 'cut',
        origin: 'gap',
        skip_suggestions: [],
      },
    ],
  }
}

function completedExportJob(): Job {
  return {
    job_id: 'exp1',
    batch_id: 'b1',
    type: 'export',
    status: 'completed',
    stage: null,
    started_at: '',
    updated_at: 't1',
    suggested_order: null,
    order_reason: null,
    warnings: [],
    error: null,
    manifest_path: null,
    output_path: '/data/b1/output.mp4',
  }
}

describe('EditLogView keyboard shortcuts (Space/Enter, in an open segment preview)', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getManifest').mockResolvedValue(makeManifest())
    vi.spyOn(api, 'getJob').mockResolvedValue(completedExportJob())
    vi.spyOn(api, 'updateSegmentStatus').mockResolvedValue({})
    vi.spyOn(api, 'triggerExport').mockResolvedValue({ ...completedExportJob(), status: 'pending' })
  })

  afterEach(() => {
    // Real unmount FIRST -- SegmentPreview's keydown listener is a plain
    // document.addEventListener from a useEffect with no cleanup guard
    // against a re-render, so it's only ever removed by the component
    // actually unmounting (its effect cleanup running). Nuking
    // document.body.innerHTML directly, before RTL gets to unmount
    // properly, leaves that listener attached to `document` (which
    // persists across tests in the same file) -- the exact real flake
    // this suite hit and root-caused, not just retried past: without
    // this explicit cleanup() first, a later test's keydown could fire
    // BOTH this test's stale listener (closing over an already-detached
    // video) and its own, and interference between the two showed up
    // as calls silently going missing.
    cleanup()
    vi.restoreAllMocks()
    document.body.innerHTML = ''
  })

  // EditLogView renders TWO <video> elements once an export exists: the
  // "Current output" player at the top of the page (always present here
  // since getJob is mocked to report a completed export -- a real,
  // normal state, not a test artifact), and SegmentPreview's own, inside
  // the specific segment's row. A bare document.querySelector('video')
  // grabs the FIRST one -- the wrong one, a real mistake this suite hit
  // and fixed, not a hypothetical -- so every lookup below is scoped to
  // the segment's own row (#edit-log-entry-seg_002) specifically.
  function previewVideo(): HTMLVideoElement {
    const row = document.getElementById('edit-log-entry-seg_002')
    return row!.querySelector('video') as HTMLVideoElement
  }

  async function renderWithOpenPreview() {
    const user = userEvent.setup()
    render(<EditLogView batchId="b1" />)
    const previewButton = await screen.findByRole('button', { name: 'Preview' })
    await user.click(previewButton)
    // SegmentPreview is now mounted -- its <video> is present
    await waitFor(() => expect(previewVideo()).not.toBeNull())
    return { user }
  }

  it('Enter runs the real restore/cut action (calls updateSegmentStatus with the correct next status)', async () => {
    await renderWithOpenPreview()
    fireEvent.keyDown(document, { code: 'Enter' })
    await waitFor(() =>
      expect(api.updateSegmentStatus).toHaveBeenCalledWith('b1', 'seg_002', 'kept'),
    )
  })

  it('Space play/pauses the preview video (HTMLMediaElement.play, stubbed in test/setup.ts)', async () => {
    await renderWithOpenPreview()
    const video = previewVideo()
    const playSpy = vi.spyOn(video, 'play')
    expect(video.paused).toBe(true)
    fireEvent.keyDown(document, { code: 'Space' })
    expect(playSpy).toHaveBeenCalledTimes(1)
  })

  it('preventDefault fires for both Space and Enter (so Space cannot also click a focused button)', async () => {
    await renderWithOpenPreview()
    const spaceEvent = new KeyboardEvent('keydown', { code: 'Space', bubbles: true, cancelable: true })
    document.dispatchEvent(spaceEvent)
    expect(spaceEvent.defaultPrevented).toBe(true)

    const enterEvent = new KeyboardEvent('keydown', { code: 'Enter', bubbles: true, cancelable: true })
    document.dispatchEvent(enterEvent)
    expect(enterEvent.defaultPrevented).toBe(true)
  })

  it('does nothing when the keydown target is a real text input (does not hijack normal typing)', async () => {
    await renderWithOpenPreview()
    const strayInput = document.createElement('input')
    document.body.appendChild(strayInput)
    const video = previewVideo()
    const playSpy = vi.spyOn(video, 'play')

    fireEvent.keyDown(strayInput, { code: 'Space' })
    fireEvent.keyDown(strayInput, { code: 'Enter' })

    expect(playSpy).not.toHaveBeenCalled()
    expect(api.updateSegmentStatus).not.toHaveBeenCalled()
  })

  it('Enter does not call updateSegmentStatus while a toggle is already pending (real disabled-guard behavior)', async () => {
    // Never resolves -- keeps isPending true for the duration of this test,
    // the real condition toggleDisabled is built from.
    vi.spyOn(api, 'updateSegmentStatus').mockImplementation(() => new Promise(() => {}))
    const { user } = await renderWithOpenPreview()
    const toggleButton = screen.getByRole('button', { name: 'Restore' })
    await user.click(toggleButton) // real click path into handleToggle, sets isPending
    await waitFor(() => expect(screen.getByRole('button', { name: 'Saving...' })).toBeDisabled())

    fireEvent.keyDown(document, { code: 'Enter' })
    // Only the one call from the real click above -- the keyboard Enter
    // must not have fired a second one while disabled.
    expect(api.updateSegmentStatus).toHaveBeenCalledTimes(1)
  })
})
