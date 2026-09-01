// Brief transitional line shown alongside the just-finished item's
// ResultStep/error card while App.tsx's startNextQueuedItem is doing
// its (usually sub-second) calibration-reuse-and-trigger work in the
// background -- so the queue's "no manual click needed between items"
// behavior doesn't feel like the screen was yanked away without
// explanation. Purely presentational.

interface AdvancingNoticeProps {
  nextName: string
}

export default function AdvancingNotice({ nextName }: AdvancingNoticeProps) {
  return (
    <p className="muted" style={{ marginTop: 16 }}>
      <span className="spinner" aria-hidden="true" /> Starting {nextName} next...
    </p>
  )
}
