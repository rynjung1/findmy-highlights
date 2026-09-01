// The multi-game queue's sidebar list -- purely presentational, all
// state and API calls live in App.tsx (same division of responsibility
// as every other step component here). Reuses the existing
// .sidebar-steps/.badge visual language from index.css rather than
// inventing a new one.

export interface QueueItem {
  batchId: string
  name: string
  status: 'queued' | 'active' | 'done' | 'error'
  // Set via the "use different calibration" toggle below -- read by
  // App.tsx's startNextQueuedItem when this item's turn comes, to skip
  // the default reuse-previous-calibration fast path for a real camera
  // change mid-queue. Meaningless once status leaves 'queued'.
  forceRecalibrate?: boolean
}

const STATUS_LABEL: Record<QueueItem['status'], string> = {
  queued: 'Queued',
  active: 'In progress',
  done: 'Done',
  error: 'Failed',
}

interface QueueListProps {
  queue: QueueItem[]
  focusedBatchId: string | null
  onFocus: (batchId: string) => void
  onToggleRecalibrate: (batchId: string) => void
}

export default function QueueList({
  queue,
  focusedBatchId,
  onFocus,
  onToggleRecalibrate,
}: QueueListProps) {
  if (queue.length === 0) return null
  return (
    <div className="sidebar-steps queue-list">
      <h3>Game queue</h3>
      {queue.map((item) => (
        <div
          key={item.batchId}
          className={`queue-item${item.batchId === focusedBatchId ? ' focused' : ''}`}
        >
          <button
            type="button"
            className="queue-item-name"
            onClick={() => onFocus(item.batchId)}
            disabled={item.status === 'queued'}
            title={item.status === 'queued' ? 'Not started yet' : `View ${item.name}`}
          >
            {item.name}
          </button>
          <span className={`badge queue-status-badge queue-status-${item.status}`}>
            {STATUS_LABEL[item.status]}
          </span>
          {item.status === 'queued' && (
            <button
              type="button"
              className="secondary pill queue-recalibrate-toggle"
              onClick={() => onToggleRecalibrate(item.batchId)}
            >
              {item.forceRecalibrate ? 'Will recalibrate' : 'Reuse calibration'}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
