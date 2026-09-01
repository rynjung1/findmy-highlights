import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Deliberately a SEPARATE config from vite.config.ts, not a merged
// `test` block added to it -- this project's real dev/build config
// stays completely untouched by adding tests. Vitest is Vite's own
// first-party test runner (reuses esbuild/the same plugin pipeline), so
// this is the natural zero-new-toolchain choice for a Vite project, not
// a second framework bolted on.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
  },
})
