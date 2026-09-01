import '@testing-library/jest-dom/vitest'

// jsdom implements neither of these -- both are exercised by real,
// shipped component logic under test (SegmentThumbnail's lazy-load
// IntersectionObserver in EditLogView.tsx; SegmentPreview's Space/Enter
// handlers call HTMLMediaElement.play()/pause(), which jsdom leaves as
// "not implemented" stubs that throw). Stubbed here, once, rather than
// per test file.

class StubIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null
  readonly rootMargin: string = ''
  readonly scrollMargin: string = ''
  readonly thresholds: ReadonlyArray<number> = []
  // Deliberately never invokes its callback -- tests that need a
  // thumbnail's lazy content to actually appear are out of scope here;
  // this exists so mounting a component that USES an observer doesn't
  // throw, not to simulate real intersection timing.
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}
globalThis.IntersectionObserver = StubIntersectionObserver

Object.defineProperty(window.HTMLMediaElement.prototype, 'play', {
  configurable: true,
  value: function play(this: HTMLMediaElement) {
    Object.defineProperty(this, 'paused', { configurable: true, value: false })
    return Promise.resolve()
  },
})
Object.defineProperty(window.HTMLMediaElement.prototype, 'pause', {
  configurable: true,
  value: function pause(this: HTMLMediaElement) {
    Object.defineProperty(this, 'paused', { configurable: true, value: true })
  },
})
Object.defineProperty(window.HTMLMediaElement.prototype, 'load', {
  configurable: true,
  value: function load() {},
})
// jsdom's own currentTime setter throws "not implemented" (reported to
// the virtual console, not the test's synchronous call stack, since
// it's set from inside a DOM event listener -- silently swallowing
// SegmentPreview's togglePlay() before it ever reaches el.play()).
// Backed by a real per-element value, not a no-op, since some tests
// check it reflects what was set.
const currentTimeValues = new WeakMap<HTMLMediaElement, number>()
Object.defineProperty(window.HTMLMediaElement.prototype, 'currentTime', {
  configurable: true,
  get(this: HTMLMediaElement) {
    return currentTimeValues.get(this) ?? 0
  },
  set(this: HTMLMediaElement, value: number) {
    currentTimeValues.set(this, value)
  },
})
