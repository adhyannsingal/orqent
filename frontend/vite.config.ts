import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: {
    port: 5173,
    // The API base URL is configurable (see `.env.example`); this proxy simply
    // makes the default local setup work with no configuration at all.
    proxy: { '/api': 'http://localhost:8000', '/hooks': 'http://localhost:8000' },
  },
})
