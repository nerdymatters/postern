/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // `.venv/bin/python -m postern.serve --port 8790` during development.
    proxy: { '/account-api': { target: 'http://127.0.0.1:8787', rewrite: path => path.replace(/^\/account-api/, '') }, '/api': 'http://127.0.0.1:8790', '/health': 'http://127.0.0.1:8790' },
  },
  build: { outDir: 'dist', sourcemap: false },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test-setup.ts'],
    include: ['src/**/*.test.tsx', 'src/**/*.test.ts'],
  },
})
