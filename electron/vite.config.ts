import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import { developmentServer } from './src/main/developmentServer.ts'

export default defineConfig(({ command }) => ({
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
  server: command === 'serve' ? {
    ...developmentServer(path.resolve(import.meta.dirname, '..')),
    strictPort: true,
  } : undefined,
}))
