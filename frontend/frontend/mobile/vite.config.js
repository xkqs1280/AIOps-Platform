import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  root: __dirname,
  plugins: [vue(), tailwindcss()],
  base: './',
  build: {
    outDir: '../../mobile_dist',
    emptyOutDir: true,
  },
})
