import { useRef, useState } from 'react'
import { previewUrl, setCalibrationCoords } from '../api'
import type { BaseName } from '../types'

interface Point {
  x: number
  y: number
}

type PointKey = 'plate' | BaseName

interface PointConfig {
  key: PointKey
  label: string
  color: string
  required: boolean
}

// Colors pulled from the existing design-system palette (index.css)
// rather than new ones invented for this -- home plate keeps the same
// --color-danger marker it always had, bases get the three other
// semantic colors already in use elsewhere in the app.
const POINTS: PointConfig[] = [
  { key: 'plate', label: 'Home plate', color: 'var(--color-danger)', required: true },
  { key: 'first', label: 'First base', color: 'var(--color-accent)', required: false },
  { key: 'second', label: 'Second base', color: 'var(--color-success)', required: false },
  { key: 'third', label: 'Third base', color: 'var(--color-warning)', required: false },
]

interface CalibrateStepProps {
  batchId: string
  onCalibrated: () => void
}

export default function CalibrateStep({ batchId, onCalibrated }: CalibrateStepProps) {
  const imgRef = useRef<HTMLImageElement>(null)
  // native = actual video pixel coordinates (what the backend needs);
  // display = on-screen position, only used to draw the marker. Keyed
  // by point (plate + up to 3 bases) so each can be marked, re-marked,
  // and cleared independently -- bases are optional, per
  // pipeline.calibration.resolve_base_zones' own "partial calibration
  // is the expected common case" contract, not something this UI needs
  // to force to completion.
  const [points, setPoints] = useState<Partial<Record<PointKey, { native: Point; display: Point }>>>({})
  const [selected, setSelected] = useState<PointKey>('plate')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  function handleClick(e: React.MouseEvent<HTMLImageElement>) {
    const img = imgRef.current
    if (!img) return
    const rect = img.getBoundingClientRect()

    if (!img.naturalWidth || !img.naturalHeight) {
      setError('Preview image has not finished loading yet -- try clicking again.')
      return
    }

    // This component deliberately never sets object-fit or a fixed
    // height on the <img> (only max-width), specifically so its
    // rendered box always has the SAME aspect ratio as the image's own
    // content -- no object-fit letterboxing, no blank padding for
    // getBoundingClientRect() to include. That's an assumption the CSS
    // has to keep honoring, not something enforced by the browser, so
    // it's checked here: if a future style change (e.g. a fixed-size
    // layout with object-fit: contain) ever breaks it, this must fail
    // loudly instead of silently scaling clicks wrong.
    const boxRatio = rect.width / rect.height
    const naturalRatio = img.naturalWidth / img.naturalHeight
    if (Math.abs(boxRatio - naturalRatio) / naturalRatio > 0.01) {
      setError(
        'Preview image is being displayed with letterboxing/padding -- ' +
          'click coordinates would be wrong. This is a display bug, not ' +
          'a calibration mistake; please report it rather than confirming.',
      )
      return
    }

    const displayX = e.clientX - rect.left
    const displayY = e.clientY - rect.top

    // THE scaling step: the browser displays this <img> at whatever
    // size fits the layout (rect.width/height), which is almost never
    // the image's actual pixel size. naturalWidth/naturalHeight are the
    // JPEG's real decoded dimensions -- guaranteed by the backend
    // (grab_preview_frame) to exactly equal the video's own frame size,
    // the same coordinate space POST /calibration validates against.
    // Sending displayX/displayY directly, without this conversion,
    // would silently produce a wrong zone any time the image isn't
    // shown at 1:1 scale -- which is effectively always.
    const scaleX = img.naturalWidth / rect.width
    const scaleY = img.naturalHeight / rect.height

    setError(null)
    setPoints((prev) => ({
      ...prev,
      [selected]: {
        native: { x: displayX * scaleX, y: displayY * scaleY },
        display: { x: displayX, y: displayY },
      },
    }))
  }

  function handleClear(key: PointKey) {
    setPoints((prev) => {
      const next = { ...prev }
      delete next[key]
      return next
    })
  }

  async function handleConfirm() {
    const plate = points.plate?.native
    if (!plate) return
    setSaving(true)
    setError(null)
    try {
      const bases: Partial<Record<BaseName, Point>> = {}
      for (const { key } of POINTS) {
        if (key === 'plate') continue
        const p = points[key]?.native
        if (p) bases[key] = p
      }
      await setCalibrationCoords(batchId, plate, bases)
      onCalibrated()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Mark home plate and bases</h2>
      <p className="muted">
        Click home plate below, then optionally select first/second/third base and click
        their positions too -- bases are optional and independent; mark only the ones
        visible in this camera angle.
      </p>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        {POINTS.map(({ key, label, color, required }) => {
          const marked = Boolean(points[key])
          const isSelected = selected === key
          return (
            <button
              key={key}
              type="button"
              className={isSelected ? 'pill' : 'secondary pill'}
              onClick={() => setSelected(key)}
            >
              <span
                style={{
                  display: 'inline-block',
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  background: marked ? color : 'transparent',
                  border: `2px solid ${color}`,
                  marginRight: 6,
                  verticalAlign: 'middle',
                }}
              />
              {label}
              {!required && !marked && <span className="muted"> (optional)</span>}
            </button>
          )
        })}
      </div>

      {points[selected] && (
        <p style={{ marginTop: -4 }}>
          <button type="button" className="secondary pill" onClick={() => handleClear(selected)}>
            Clear {POINTS.find((p) => p.key === selected)?.label}
          </button>
        </p>
      )}

      <div
        style={{
          position: 'relative',
          display: 'inline-block',
          borderRadius: 'var(--radius-md)',
          overflow: 'hidden',
          boxShadow: 'var(--shadow-sm)',
          border: '1px solid var(--color-border)',
        }}
      >
        <img
          ref={imgRef}
          src={previewUrl(batchId)}
          alt="Calibration preview frame"
          onClick={handleClick}
          style={{ maxWidth: '100%', cursor: 'crosshair', display: 'block' }}
        />
        {POINTS.map(({ key, color }) => {
          const p = points[key]
          if (!p) return null
          const isSelected = selected === key
          return (
            <div
              key={key}
              style={{
                position: 'absolute',
                left: p.display.x - 9,
                top: p.display.y - 9,
                width: 18,
                height: 18,
                borderRadius: '50%',
                border: `3px solid ${color}`,
                boxShadow: isSelected
                  ? `0 0 0 3px ${color}33, 0 0 0 1px white`
                  : '0 0 0 1px white',
                pointerEvents: 'none',
              }}
            />
          )
        })}
      </div>
      {error && <p className="alert alert-danger">{error}</p>}
      <p>
        <button onClick={handleConfirm} disabled={!points.plate || saving}>
          {saving ? 'Saving...' : 'Confirm calibration'}
        </button>
      </p>
    </div>
  )
}
