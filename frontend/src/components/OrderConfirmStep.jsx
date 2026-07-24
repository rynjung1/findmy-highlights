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
    <div>
      <h2>Confirm file order</h2>
      <p>
        These files couldn't be ordered automatically: <em>{reason}</em>. Put
        them in the order they were actually recorded.
      </p>
      <ol>
        {order.map((name, i) => (
          <li key={name} style={{ marginBottom: 4 }}>
            {name}{' '}
            <button className="secondary" onClick={() => move(i, -1)} disabled={i === 0}>
              ↑
            </button>{' '}
            <button
              className="secondary"
              onClick={() => move(i, 1)}
              disabled={i === order.length - 1}
            >
              ↓
            </button>
          </li>
        ))}
      </ol>
      {error && <p style={{ color: 'red' }}>{error}</p>}
      <button onClick={handleConfirm} disabled={saving}>
        {saving ? 'Confirming...' : 'Confirm order and process'}
      </button>
    </div>
  )
}
