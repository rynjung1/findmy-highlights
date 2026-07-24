import { useRef, useState } from 'react'
import { previewUrl, setCalibrationCoords } from '../api'

export default function CalibrateStep({ batchId, onCalibrated }) {
  const imgRef = useRef(null)
  // native = actual video pixel coordinates (what the backend needs);
  // display = on-screen position, only used to draw the marker
  const [native, setNative] = useState(null)
  const [display, setDisplay] = useState(null)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  function handleClick(e) {
    const img = imgRef.current
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
    // would silently produce a wrong plate zone any time the image
    // isn't shown at 1:1 scale -- which is effectively always.
    const scaleX = img.naturalWidth / rect.width
    const scaleY = img.naturalHeight / rect.height

    setError(null)
    setNative({ x: displayX * scaleX, y: displayY * scaleY })
    setDisplay({ x: displayX, y: displayY })
  }

  async function handleConfirm() {
    if (!native) return
    setSaving(true)
    setError(null)
    try {
      await setCalibrationCoords(batchId, native.x, native.y)
      onCalibrated()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <h2>2. Mark home plate</h2>
      <p>Click the center of home plate in the frame below.</p>
      <div style={{ position: 'relative', display: 'inline-block' }}>
        <img
          ref={imgRef}
          src={previewUrl(batchId)}
          alt="Calibration preview frame"
          onClick={handleClick}
          style={{ maxWidth: '100%', cursor: 'crosshair', display: 'block' }}
        />
        {display && (
          <div
            style={{
              position: 'absolute',
              left: display.x - 7,
              top: display.y - 7,
              width: 14,
              height: 14,
              borderRadius: '50%',
              border: '3px solid red',
              boxShadow: '0 0 0 1px white',
              pointerEvents: 'none',
            }}
          />
        )}
      </div>
      {error && <p style={{ color: 'red' }}>{error}</p>}
      <p>
        <button onClick={handleConfirm} disabled={!native || saving}>
          {saving ? 'Saving...' : 'Confirm plate location'}
        </button>
      </p>
    </div>
  )
}
