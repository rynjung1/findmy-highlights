import { useEffect, useState } from 'react'
import UploadStep from './components/UploadStep'
import CalibrateStep from './components/CalibrateStep'
import OrderConfirmStep from './components/OrderConfirmStep'
import ProcessingStep from './components/ProcessingStep'
import ResultStep from './components/ResultStep'
import EditLogView from './components/EditLogView'
import ReviewQueueView from './components/ReviewQueueView'
import { getJob, triggerProcess } from './api'

const STORAGE_KEY = 'fmh_batch_id'

// Stages: loading -> upload -> calibrate -> [order_confirm ->] processing -> done
//                                                                        \-> error
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

export default function App() {
  const [view, setView] = useState<View>('home') // independent of the stage machine below
  const [stage, setStage] = useState<Stage>('loading')
  const [batchId, setBatchId] = useState<string | null>(null)
  const [orderInfo, setOrderInfo] = useState<OrderInfo | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  // On mount: resume from wherever the batch actually is on the server,
  // never assume a fresh client. Job state is durable server-side (see
  // backend/jobs.py) specifically so a reload or a closed tab mid-run
  // doesn't lose the user's place -- this is the other half of that:
  // the client has to actually ask, not just start over at "upload".
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (!saved) {
      setStage('upload')
      return
    }
    setBatchId(saved)
    resumeFromServer(saved)
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
      setStage('error')
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : String(err))
      setStage('error')
    }
  }

  function handleUploaded(id: string) {
    localStorage.setItem(STORAGE_KEY, id)
    setBatchId(id)
    setStage('calibrate')
  }

  // Demo mode's /demo/run already calibrates and triggers processing
  // server-side in one call (see backend/demo.py) -- unlike a real
  // upload, this jumps straight to 'processing', skipping 'calibrate'
  // entirely, since there's nothing left for the user to do before the
  // job that's already running.
  function handleDemoStarted(id: string) {
    localStorage.setItem(STORAGE_KEY, id)
    setBatchId(id)
    setStage('processing')
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
      setStage('error')
    }
  }

  function handleOrderConfirmed() {
    setStage('processing')
  }

  function handleStartOver() {
    localStorage.removeItem(STORAGE_KEY)
    setBatchId(null)
    setOrderInfo(null)
    setErrorMessage(null)
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
      </aside>

      <main className="main-content">
      <div className="app-shell">
        {view === 'editlog' && <EditLogView batchId={batchId} />}
        {view === 'review' && <ReviewQueueView />}

        {view === 'home' && (
        <>
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
              onDone={() => setStage('done')}
              onError={(msg: string) => {
                setErrorMessage(msg)
                setStage('error')
              }}
            />
          )}
          {stage === 'done' && batchId && <ResultStep batchId={batchId} />}
          {stage === 'error' && (
            <div className="card">
              <h2>Something went wrong</h2>
              <p className="alert alert-danger">{errorMessage}</p>
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
