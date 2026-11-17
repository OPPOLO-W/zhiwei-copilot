import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发期与预览期都把 /api 代理到本地 FastAPI，避免前端写死后端地址、也避免 CORS 配置漂移
const proxy = {
  "/api": {
    target: process.env.VITE_API_TARGET || "http://127.0.0.1:8000",
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy,
  },
  // vite preview 只认 preview.proxy，不继承 server.proxy；这里显式补一份，
  // 使 `npm run build && npm run preview` 也能正常打到后端
  preview: {
    port: 5173,
    proxy,
  },
  build: { outDir: "dist", sourcemap: false },
});
