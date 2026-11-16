import { fileURLToPath, URL } from 'node:url';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

/**
 * The dev server proxies `/api` to the public API so the browser never needs
 * CORS. Default target is the NestJS API (full mode); point
 * VITE_API_PROXY_TARGET at http://localhost:8000 for standalone mode.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxyTarget =
    env.VITE_API_PROXY_TARGET || process.env.VITE_API_PROXY_TARGET || 'http://localhost:3000';

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    optimizeDeps: {
      exclude: ['@pdf2x/contracts'],
    },
    server: {
      port: 5173,
      strictPort: false,
      fs: {
        // The contracts package is linked from ../../packages/contracts.
        allow: [fileURLToPath(new URL('.', import.meta.url)), fileURLToPath(new URL('../..', import.meta.url))],
      },
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
          ws: false,
          // SSE must not be buffered.
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.setHeader('Accept-Encoding', 'identity');
            });
          },
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      chunkSizeWarningLimit: 900,
    },
  };
});
