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
    // 必须显式绑到 IPv4 回环。默认值 "localhost" 在 Node 17+ 只会解析到 ::1，
    // 于是 http://127.0.0.1:5173 一律连接被拒——浏览器自动化、健康检查、
    // curl 全部报错，表现出来就像 dev server「崩了」，实际进程一直活着。
    // 绑 127.0.0.1 后，127.0.0.1 直连可用，localhost 也会回落到 IPv4。
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
