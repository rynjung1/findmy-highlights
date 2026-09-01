import { useEffect, useState } from 'react'
import UploadStep from './components/UploadStep'
import CalibrateStep from './components/CalibrateStep'
import OrderConfirmStep from './components/OrderConfirmStep'
import ProcessingStep from './components/ProcessingStep'
import ResultStep from './components/ResultStep'
import EditLogView from './components/EditLogView'
import ReviewQueueView from './components/ReviewQueueView'
import QueueList, { type QueueItem } from './components/QueueList'
import AdvancingNotice from './components/AdvancingNotice'
import {
  AppError,
  type AppErrorKind,
  getCalibration,
  getJob,
  setCalibrationFile,
  triggerProcess,
} from './api'

// Was a single string (one batch id) before the multi-game queue --
// now an array so the whole queue (not just "the current one") survives
// a reload, same "resume from wherever the server actually says, never
// assume" philosophy the single-batch version already had, generalized
// to N items instead of 1.
const STORAGE_KEY = 'fmh_batch_queue'

// Stages: loading -> upload -> calibrate -> [order_confirm ->] processing -> done
//                                                                        \-> error
// Unchanged from the single-batch version -- these describe the FOCUSED
// queue item's state; the queue array (below) tracks every item.
type Stage =
  | 'loading'
  | 'upload'
  | 'calibrate'
  | 'order_confirm'
  | 'processing'
  | 'done'
  | 'error'

type View = 'home' | 'editlog' | 'review'

interface OrderInfo {
  suggestedOrder: string[]
  reason: string | null
}

// order_confirm is a sub-state that only sometimes appears between
// calibrate and processing -- it's folded into the "Process" tracker
// step rather than given its own, since the tracker's fixed 4 steps
// are meant to show overall progress, not every possible server state.
const TRACKER_STEPS = [
  { label: 'Upload', desc: 'Add your recordings' },
  { label: 'Calibrate', desc: 'Mark home plate' },
  { label: 'Process', desc: 'Detect and cut highlights' },
  { label: 'Done', desc: 'Watch and download' },
]
const STAGE_TO_STEP_INDEX: Partial<Record<Stage, number>> = {
  upload: 0,
  calibrate: 1,
  order_confirm: 2,
  processing: 2,
  done: 3,
}

function SidebarSteps({ stage }: { stage: Stage }) {
  const currentIndex = STAGE_TO_STEP_INDEX[stage]
  if (currentIndex === undefined) return null
  return (
    <div className="sidebar-steps">
      <h3>This upload</h3>
      {TRACKER_STEPS.map(({ label, desc }, i) => {
        const status = i < currentIndex ? 'done' : i === currentIndex ? 'current' : ''
        return (
          <div key={label} className={`sidebar-step${status ? ` ${status}` : ''}`}>
            <div className="sidebar-step-rail">
              <span className="sidebar-step-dot">{i < currentIndex ? '✓' : i + 1}</span>
              {i < TRACKER_STEPS.length - 1 && (
                <span className={`sidebar-step-line${i < currentIndex ? ' done' : ''}`} />
              )}
            </div>
            <div className="sidebar-step-text">
              <div className="sidebar-step-label">{label}</div>
              <div className="sidebar-step-desc">{desc}</div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function loadQueue(): QueueItem[] {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return []
  try {
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function saveQueue(items: QueueItem[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(items))
}

export default function App() {
  const [view, setView] = useState<View>('home') // independent of the stage machine below
  const [stage, setStage] = useState<Stage>('loading')
  const [batchId, setBatchId] = useState<string | null>(null)
  const [orderInfo, setOrderInfo] = useState<OrderInfo | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  // 'network' gets a real Retry action below (a transient backend blip
  // is worth trying again); 'disk_full' doesn't (retrying changes
  // nothing until someone actually frees space on the server) -- see
  // docs/INVESTIGATION_LOG.md for the two real incidents this
  // distinction comes from. Defaults to 'server' for anything not an
  // AppError (a plain thrown string, etc.), which keeps today's retry
  // behavior for those rather than silently dropping the option.
  const [errorKind, setErrorKind] = useState<AppErrorKind>('server')

  // The multi-game queue. `stage`/`batchId` above always describe the
  // FOCUSED item (whichever one is currently on screen); `queue` tracks
  // every item's own status so the sidebar can show all of them and so
  // a finished/failed item stays reachable after the app has moved on.
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [showAddGame, setShowAddGame] = useState(false)
  // Set only while startNextQueuedItem's calibration-reuse-and-trigger
  // call is in flight for the item right after `batchId` -- lets the
  // just-finished item's ResultStep/error card keep showing (unchanged,
  // per the queue's own design) with a brief explanatory line under it,
  // rather than the screen silently jumping to the next item.
  const [advancingToName, setAdvancingToName] = useState<string | null>(null)

  function markQueueItem(id: string, patch: Partial<QueueItem>) {
    setQueue((prev) => {
      const next = prev.map((item) => (item.batchId === id ? { ...item, ...patch } : item))
      saveQueue(next)
      return next
    })
  }

  function addQueueItem(item: QueueItem) {
    setQueue((prev) => {
      const next = [...prev, item]
      saveQueue(next)
      return next
    })
  }

  // On mount: resume from wherever the FOCUSED batch actually is on the
  // server, never assume a fresh client -- same reasoning as before,
  // now applied to whichever queue item was active (or, if none was,
  // the last one added) when the tab was last open. Job state is
  // durable server-side (see backend/jobs.py) specifically so a reload
  // or a closed tab mid-run doesn't lose the user's place.
  useEffect(() => {
    const saved = loadQueue()
    if (saved.length === 0) {
      setStage('upload')
      return
    }
    setQueue(saved)
    const focus = saved.find((i) => i.status === 'active') ?? saved[saved.length - 1]
    setBatchId(focus.batchId)
    resumeFromServer(focus.batchId)
  }, [])

  async function resumeFromServer(id: string) {
    try {
      const detect = await getJob(id, 'detect')
      if (!detect) {
        setStage('calibrate')
        return
      }
      if (detect.status === 'needs_order_confirmation') {
        setOrderInfo({ suggestedOrder: detect.suggested_order ?? [], reason: detect.order_reason })
        setStage('order_confirm')
        return
      }
      if (detect.status === 'pending' || detect.status === 'in_progress') {
        setStage('processing')
        return
      }
      if (detect.status === 'completed') {
        const exp = await getJob(id, 'export')
        setStage(exp?.status === 'completed' ? 'done' : 'processing')
        return
      }
      // failed / interrupted
      setErrorMessage(
        `The previous run for this upload didn't finish (${detect.status}): ` +
          `${detect.error || 'no error detail available'}`,
      )
      setErrorKind('server')
      setStage('error')
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : String(err))
      setErrorKind(err instanceof AppError ? err.kind : 'server')
      setStage('error')
    }
  }

  function handleUploaded(id: string) {
    const item: QueueItem = { batchId: id, name: `Game ${queue.length + 1}`, status: 'active' }
    addQueueItem(item)
    setBatchId(id)
    setStage('calibrate')
  }

  // Demo mode's /demo/run already calibrates and triggers processing
  // server-side in one call (see backend/demo.py) -- unlike a real
  // upload, this jumps straight to 'processing', skipping 'calibrate'
  // entirely, since there's nothing left for the user to do before the
  // job that's already running. Still joins the queue as item 1 so a
  // real game can be queued up after trying the demo.
  function handleDemoStarted(id: string) {
    const item: QueueItem = { batchId: id, name: 'Demo', status: 'active' }
    addQueueItem(item)
    setBatchId(id)
    setStage('processing')
  }

  // "Add another game": uses the SAME UploadStep component (compact
  // mode) as the first upload, but only ever appends a new queued item
  // -- it never touches the focused stage/batchId, so whatever the user
  // is currently looking at (the active item's calibrate/processing/
  // done screen) is completely undisturbed.
  function handleAddedToQueue(id: string) {
    addQueueItem({ batchId: id, name: `Game ${queue.length + 1}`, status: 'queued' })
    setShowAddGame(false)
  }

  function handleToggleRecalibrate(id: string) {
    const current = queue.find((i) => i.batchId === id)
    markQueueItem(id, { forceRecalibrate: !current?.forceRecalibrate })
  }

  function handleFocus(id: string) {
    setBatchId(id)
    setStage('loading')
    setOrderInfo(null)
    setErrorMessage(null)
    resumeFromServer(id)
  }

  // The queue's outer loop: called once the focused item reaches a
  // terminal state (done/error, from onDone/onError below). Advances to
  // the next 'queued' item WITHOUT a manual click in the common case
  // (reusing the just-finished item's calibration -- see api.ts's
  // setCalibrationFile), automatically. Reaching a triggered detect job
  // at all already requires a calibration.json to exist (the backend
  // rejects /process otherwise unless allow_uncalibrated is explicitly
  // passed, which this app never does -- confirmed live: even a demo
  // run seeds a real one, see backend/demo.py), so in normal use the
  // reuse lookup below essentially always succeeds. The two real ways
  // to land on the normal CalibrateStep instead are the explicit,
  // user-driven forceRecalibrate toggle (a real camera change,
  // QueueList's per-item control) and, defensively, a genuinely missing
  // calibration.json if this app is ever driven partly through the raw
  // API outside this UI -- both handled the same way, without guessing.
  async function startNextQueuedItem(finishedBatchId: string) {
    const next = queue.find((i) => i.status === 'queued')
    if (!next) return

    setAdvancingToName(next.name)
    markQueueItem(next.batchId, { status: 'active' })
    try {
      let reused = false
      if (!next.forceRecalibrate) {
        const prevCalibration = await getCalibration(finishedBatchId)
        if (prevCalibration) {
          await setCalibrationFile(next.batchId, prevCalibration)
          await triggerProcess(next.batchId)
          reused = true
        }
      }
      setBatchId(next.batchId)
      setOrderInfo(null)
      setStage(reused ? 'processing' : 'calibrate')
    } catch (err) {
      // A failure here (e.g. the reuse call itself hit a network blip)
      // still needs to land the new item somewhere real rather than
      // hang silently -- same error-classification path every other
      // handler in this file already uses.
      markQueueItem(next.batchId, {
        status: 'error',
        forceRecalibrate: next.forceRecalibrate,
      })
      setBatchId(next.batchId)
      setErrorMessage(err instanceof Error ? err.message : String(err))
      setErrorKind(err instanceof AppError ? err.kind : 'server')
      setStage('error')
    } finally {
      setAdvancingToName(null)
    }
  }

  async function handleCalibrated() {
    // Stage flips to 'processing' only AFTER the trigger call resolves,
    // not before it's even sent -- ProcessingStep starts polling
    // GET /jobs/detect the instant it mounts, so mounting it earlier
    // guarantees that first poll asks for a job that doesn't exist on
    // the server yet (a real, always-reproducible 404, not a rare
    // race). CalibrateStep's own "Saving..." button state already
    // covers this brief extra wait, so there's no UX gap.
    if (!batchId) return
    try {
      const job = await triggerProcess(batchId)
      if (job.status === 'needs_order_confirmation') {
        setOrderInfo({ suggestedOrder: job.suggested_order ?? [], reason: job.order_reason })
        setStage('order_confirm')
      } else {
        setStage('processing')
      }
      // otherwise ProcessingStep's own polling takes over from here
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : String(err))
      setErrorKind(err instanceof AppError ? err.kind : 'server')
      setStage('error')
    }
  }

  function handleOrderConfirmed() {
    setStage('processing')
  }

  // Re-asks the server where this batch actually stands, rather than
  // just clearing the error and hoping -- covers both real error
  // sources that route here (the mount-time resume check and
  // ProcessingStep's own onError once ITS internal retry tolerance is
  // exhausted) with one path: if the server's back, this naturally
  // lands on whatever stage it should be in now; if it's still down,
  // it fails again with the same honest message, no worse off.
  function handleRetry() {
    if (!batchId) return
    setErrorMessage(null)
    resumeFromServer(batchId)
  }

  function handleStartOver() {
    localStorage.removeItem(STORAGE_KEY)
    setQueue([])
    setBatchId(null)
    setOrderInfo(null)
    setErrorMessage(null)
    setShowAddGame(false)
    setStage('upload')
  }

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="app-header">
          <span className="app-mark" aria-hidden="true">FH</span>
          <h1 className="app-title">Find My Highlights</h1>
        </div>

        <nav className="app-nav">
          <button
            className={view === 'home' ? 'active' : 'secondary'}
            onClick={() => setView('home')}
          >
            Home
          </button>
          <button
            className={view === 'editlog' ? 'active' : 'secondary'}
            onClick={() => setView('editlog')}
          >
            Edit Log
          </button>
          <button
            className={view === 'review' ? 'active' : 'secondary'}
            onClick={() => setView('review')}
          >
            Review Queue
          </button>
        </nav>

        {view === 'home' && stage !== 'loading' && stage !== 'error' && (
          <SidebarSteps stage={stage} />
        )}

        {view === 'home' && (
          <>
            <QueueList
              queue={queue}
              focusedBatchId={batchId}
              onFocus={handleFocus}
              onToggleRecalibrate={handleToggleRecalibrate}
            />
            {queue.length > 0 && !showAddGame && (
              <button
                type="button"
                className="secondary"
                style={{ marginTop: 14 }}
                onClick={() => setShowAddGame(true)}
              >
                + Add another game
              </button>
            )}
          </>
        )}
      </aside>

      <main className="main-content">
      <div className="app-shell">
        {view === 'editlog' && <EditLogView batchId={batchId} />}
        {view === 'review' && <ReviewQueueView />}

        {view === 'home' && (
        <>
          {showAddGame && (
            <div className="card" style={{ marginBottom: 20 }}>
              <h2 style={{ marginTop: 0 }}>Add another game to the queue</h2>
              <p className="muted">
                Uploads now and joins the queue -- it'll start automatically once
                the current game finishes, reusing this session's calibration if
                the camera hasn't moved.
              </p>
              <UploadStep onUploaded={handleAddedToQueue} onDemoStarted={() => {}} compact />
              <p style={{ marginTop: 12 }}>
                <button className="secondary" onClick={() => setShowAddGame(false)}>
                  Cancel
                </button>
              </p>
            </div>
          )}

          {stage === 'loading' && (
            <div className="card">
              <span className="spinner" aria-hidden="true" />
              Loading...
            </div>
          )}
          {stage === 'upload' && (
            <UploadStep onUploaded={handleUploaded} onDemoStarted={handleDemoStarted} />
          )}
          {stage === 'calibrate' && batchId && (
            <CalibrateStep batchId={batchId} onCalibrated={handleCalibrated} />
          )}
          {stage === 'order_confirm' && batchId && orderInfo && (
            <OrderConfirmStep
              batchId={batchId}
              suggestedOrder={orderInfo.suggestedOrder}
              reason={orderInfo.reason}
              onConfirmed={handleOrderConfirmed}
            />
          )}
          {stage === 'processing' && batchId && (
            <ProcessingStep
              batchId={batchId}
              onDone={() => {
                markQueueItem(batchId, { status: 'done' })
                setStage('done')
                void startNextQueuedItem(batchId)
              }}
              onError={(msg: string, kind: AppErrorKind = 'server') => {
                markQueueItem(batchId, { status: 'error' })
                setErrorMessage(msg)
                setErrorKind(kind)
                setStage('error')
                void startNextQueuedItem(batchId)
              }}
            />
          )}
          {stage === 'done' && batchId && (
            <>
              <ResultStep batchId={batchId} />
              {advancingToName && <AdvancingNotice nextName={advancingToName} />}
            </>
          )}
          {stage === 'error' && (
            <div className="card">
              <h2>
                {errorKind === 'network'
                  ? "Can't reach the server"
                  : errorKind === 'disk_full'
                    ? 'Server is out of disk space'
                    : 'Something went wrong'}
              </h2>
              <p className="alert alert-danger">{errorMessage}</p>
              {errorKind === 'disk_full' ? (
                <p className="muted">
                  This won't resolve on its own -- someone needs to free up
                  space on the server first.
                </p>
              ) : (
                batchId && (
                  <p>
                    <button onClick={handleRetry}>Try again</button>
                  </p>
                )
              )}
              {advancingToName && <AdvancingNotice nextName={advancingToName} />}
            </div>
          )}

          {stage !== 'upload' && stage !== 'loading' && (
            <p style={{ marginTop: 20 }}>
              <button className="secondary" onClick={handleStartOver}>
                Start over with a new upload
              </button>
            </p>
          )}
        </>
        )}
      </div>
      </main>
    </div>
  )
}
