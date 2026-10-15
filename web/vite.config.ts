import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // `npm run dev` talks to the API running in Compose. In production nginx
    // does the same proxying, so the browser only ever sees one origin and
    // there is no CORS preflight on either path.
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true, ws: true },
      '/healthz': { target: 'http://localhost:8000', changeOrigin: true },
      '/readyz': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
})
