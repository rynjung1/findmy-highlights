import { useEffect, useState } from 'react'
import UploadStep from './components/UploadStep'
import CalibrateStep from './components/CalibrateStep'
import OrderConfirmStep from './components/OrderConfirmStep'
import ProcessingStep from './components/ProcessingStep'
import ResultStep from './components/ResultStep'
import EditLogView from './components/EditLogView'
import { getJob, triggerProcess } from './api'

const STORAGE_KEY = 'fmh_batch_id'

// Stages: loading -> upload -> calibrate -> [order_confirm ->] processing -> done
//                                                                        \-> error
export default function App() {
  const [view, setView] = useState('home') // home | editlog -- independent of the stage machine below
  const [stage, setStage] = useState('loading')
  const [batchId, setBatchId] = useState(null)
  const [orderInfo, setOrderInfo] = useState(null)
  const [errorMessage, setErrorMessage] = useState(null)

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

  async function resumeFromServer(id) {
    try {
      const detect = await getJob(id, 'detect')
      if (!detect) {
        setStage('calibrate')
        return
      }
      if (detect.status === 'needs_order_confirmation') {
        setOrderInfo({ suggestedOrder: detect.suggested_order, reason: detect.order_reason })
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
      setErrorMessage(err.message)
      setStage('error')
    }
  }

  function handleUploaded(id) {
    localStorage.setItem(STORAGE_KEY, id)
    setBatchId(id)
    setStage('calibrate')
  }

  async function handleCalibrated() {
    // Stage flips to 'processing' only AFTER the trigger call resolves,
    // not before it's even sent -- ProcessingStep starts polling
    // GET /jobs/detect the instant it mounts, so mounting it earlier
    // guarantees that first poll asks for a job that doesn't exist on
    // the server yet (a real, always-reproducible 404, not a rare
    // race). CalibrateStep's own "Saving..." button state already
    // covers this brief extra wait, so there's no UX gap.
    try {
      const job = await triggerProcess(batchId)
      if (job.status === 'needs_order_confirmation') {
        setOrderInfo({ suggestedOrder: job.suggested_order, reason: job.order_reason })
        setStage('order_confirm')
      } else {
        setStage('processing')
      }
      // otherwise ProcessingStep's own polling takes over from here
    } catch (err) {
      setErrorMessage(err.message)
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
    <div style={{ maxWidth: 900, margin: '40px auto', fontFamily: 'sans-serif', padding: 16 }}>
      <h1>Find My Highlights</h1>

      <nav style={{ marginBottom: 24, borderBottom: '1px solid #ddd', paddingBottom: 12 }}>
        <button
          className={view === 'home' ? '' : 'secondary'}
          onClick={() => setView('home')}
          style={{ marginRight: 8 }}
        >
          Home
        </button>
        <button
          className={view === 'editlog' ? '' : 'secondary'}
          onClick={() => setView('editlog')}
        >
          Edit Log
        </button>
      </nav>

      {view === 'editlog' && <EditLogView batchId={batchId} />}

      {view === 'home' && (
        <>
          {stage === 'loading' && <p>Loading...</p>}
          {stage === 'upload' && <UploadStep onUploaded={handleUploaded} />}
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
              onError={(msg) => {
                setErrorMessage(msg)
                setStage('error')
              }}
            />
          )}
          {stage === 'done' && batchId && <ResultStep batchId={batchId} />}
          {stage === 'error' && (
            <div>
              <p style={{ color: 'red' }}>{errorMessage}</p>
            </div>
          )}

          {stage !== 'upload' && stage !== 'loading' && (
            <p style={{ marginTop: 24 }}>
              <button className="secondary" onClick={handleStartOver}>
                Start over with a new upload
              </button>
            </p>
          )}
        </>
      )}
    </div>
  )
}
