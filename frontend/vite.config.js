import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-server proxy to the backend so the frontend never needs CORS
// handling or a hardcoded absolute URL -- /batches/* just works whether
// you're running `npm run dev` or (later) a production build served by
// something that proxies the same way.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/batches': 'http://127.0.0.1:8420',
    },
  },
})
