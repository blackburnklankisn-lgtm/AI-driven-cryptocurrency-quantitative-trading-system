import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // base: './' is required for Electron production builds.
  // When Electron loads index.html from file://, absolute paths ('/assets/...')
  // fail. Relative paths ('./assets/...') work correctly.
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },

  base: './',
})
