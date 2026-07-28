import { useState } from 'react'
import { confirmOrder } from '../api'

export default function OrderConfirmStep({ batchId, suggestedOrder, reason, onConfirmed }) {
  const [order, setOrder] = useState(suggestedOrder)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  function move(index, delta) {
    const target = index + delta
    if (target < 0 || target >= order.length) return
    const next = [...order]
    ;[next[index], next[target]] = [next[target], next[index]]
    setOrder(next)
  }

  async function handleConfirm() {
    setSaving(true)
    setError(null)
    try {
      await confirmOrder(batchId, order)
      onConfirmed()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card">
      <span className="step-eyebrow">Order needs review</span>
      <h2 style={{ marginTop: 0 }}>Confirm file order</h2>
      <p className="muted">
        These files couldn't be ordered automatically: <em>{reason}</em>. Put
        them in the order they were actually recorded.
      </p>
      <ol style={{ listStyle: 'none', padding: 0 }}>
        {order.map((name, i) => (
          <li
            key={name}
            className="entry-card"
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 12,
            }}
          >
            <span>
              <span className="badge badge-neutral" style={{ marginRight: 10 }}>
                {i + 1}
              </span>
              {name}
            </span>
            <span>
              <button className="secondary pill" onClick={() => move(i, -1)} disabled={i === 0}>
                ↑
              </button>{' '}
              <button
                className="secondary pill"
                onClick={() => move(i, 1)}
                disabled={i === order.length - 1}
              >
                ↓
              </button>
            </span>
          </li>
        ))}
      </ol>
      {error && <p className="alert alert-danger">{error}</p>}
      <button onClick={handleConfirm} disabled={saving}>
        {saving ? 'Confirming...' : 'Confirm order and process'}
      </button>
    </div>
  )
}
