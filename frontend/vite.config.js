import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-server proxy to the backend so local dev never needs CORS handling
// or a hardcoded absolute URL -- /batches/*, /demo/*, and /review/* just
// work under `npm run dev` with no VITE_API_BASE_URL set (see src/api.js).
// A real deployment with the frontend and backend on different domains
// sets VITE_API_BASE_URL at build time instead -- this proxy only ever
// applies to the dev server, not a production build, so it needs no
// changes for that case. See README's Deployment section.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/batches': 'http://127.0.0.1:8420',
      '/demo': 'http://127.0.0.1:8420',
      '/review': 'http://127.0.0.1:8420',
    },
  },
})
