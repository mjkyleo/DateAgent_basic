import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/* 开发期 /api 直连后端（basic/main.py 默认 8001，CORS 白名单已含 5174）；
   生产由同一域名下的反向代理提供 /api，这里不写死域名。 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8001', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8001', changeOrigin: true },
    },
  },
  build: {
    // echarts 体积本就偏大，按需引入后仍会超默认阈值，这里放宽告警
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // 按"变更频率"分包：业务代码常改，react/echarts 基本不动，便于长效缓存
        manualChunks: {
          react: ['react', 'react-dom'],
          echarts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'],
        },
      },
    },
  },
})
