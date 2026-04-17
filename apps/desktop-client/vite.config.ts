import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // base: './' is required for Electron production builds.
  // When Electron loads index.html from file://, absolute paths ('/assets/...')
  // fail. Relative paths ('./assets/...') work correctly.
  base: './',
})
