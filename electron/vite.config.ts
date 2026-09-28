import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  root: '.',
  base: './',
  publicDir: path.resolve(import.meta.dirname, '../assets/icons/app'),
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, 'src/renderer'),
      '@assets': path.resolve(import.meta.dirname, '../assets'),
    },
  },
  build: {
    outDir: 'dist/renderer',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
})
