import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发态把 /api 代理到 FastAPI 后端（默认 :8000）。
// 后端已开 CORS(allow_origins=*)，所以也可跨端口直连；代理模式更贴近生产同源部署。
const API_TARGET = process.env.API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
