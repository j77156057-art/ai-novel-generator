import { fileURLToPath, URL } from "node:url";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

/**
 * 组件级测试配置（与 `vite.config.ts` 分开）：
 *
 * - 只收 tests/vitest 目录下的 `.test.ts`，避免 vitest 去跑 `node --test` 的
 *   `.mjs` 文件（两套 test runner 混跑会直接报错）；
 * - alias 与 `vite.config.ts` 保持一致（`@ -> src`），否则组件里的 `@/api/*`
 *   在测试环境解析不到；
 * - `happy-dom` 比 jsdom 轻，够跑 Vue 组件与 DOM 事件。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "happy-dom",
    include: ["tests/vitest/**/*.test.ts"],
    setupFiles: ["tests/vitest/setup.ts"],
    restoreMocks: true,
  },
});
