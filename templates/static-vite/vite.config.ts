import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  root: 'source',
  build: {
    outDir: '../dist',
    emptyOutDir: true,
  }
});
