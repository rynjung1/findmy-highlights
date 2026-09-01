import { describe, expect, it } from 'vitest'
import { AppError, classifyMessage } from './api'

// Real cases already verified live tonight against a running backend --
// see docs/INVESTIGATION_LOG.md's real ECONNREFUSED and disk-full
// incidents, and the "Server is out of disk space" / "Can't reach the
// server" error screens ProcessingStep/App.tsx render from these.
describe('classifyMessage', () => {
  it('classifies a real ENOSPC job.error string as disk_full, with a friendly message', () => {
    const raw = "[Errno 28] No space left on device: '/data/uploads/abc/output.mp4'"
    const result = classifyMessage(raw)
    expect(result.kind).toBe('disk_full')
    expect(result.message).toContain('out of disk space')
    expect(result.message).not.toBe(raw) // the raw Python OSError string must not reach the UI verbatim
  })

  it('matches "no space left on device" case-insensitively', () => {
    expect(classifyMessage('NO SPACE LEFT ON DEVICE').kind).toBe('disk_full')
  })

  it('matches a bare "ENOSPC" mention', () => {
    expect(classifyMessage('write failed: ENOSPC').kind).toBe('disk_full')
  })

  it('classifies an ordinary server-side error detail as server, unchanged', () => {
    const raw = 'no calibration set for this batch'
    const result = classifyMessage(raw)
    expect(result.kind).toBe('server')
    expect(result.message).toBe(raw)
  })

  it('classifies a real detection-failure error message as server, not disk_full', () => {
    const raw = 'could not open video: /data/uploads/xyz/corrupt.mkv'
    expect(classifyMessage(raw).kind).toBe('server')
  })
})

describe('AppError', () => {
  it('carries its kind and message as a real Error subclass', () => {
    const err = new AppError('the message', 'network')
    expect(err).toBeInstanceOf(Error)
    expect(err.name).toBe('AppError')
    expect(err.message).toBe('the message')
    expect(err.kind).toBe('network')
  })
})
