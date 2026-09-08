import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],

  // The page is served by Flask at /app, so every asset URL in the built
  // index.html has to be written relative to that. Left at the default the
  // bundle asks for /assets/... , which is not a route the dashboard has,
  // and the page loads as a blank white screen with two 404s in the console
  // and nothing in the Python log.
  base: '/app/',

  // Straight into the Python package, so `Dashboard.bat` serves the new page
  // with no build step on Lyon's machine. He has no Node and should not need
  // one: the built files are committed, and this command is only ever run
  // here.
  build: {
    outDir: path.resolve(__dirname, '../src/outreach/web'),
    emptyOutDir: true,
  },
});
