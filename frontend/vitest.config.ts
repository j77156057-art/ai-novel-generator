import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

/**
 * 前端**组件级**测试（vitest + @vue/test-utils + jsdom）。
 *
 * 与 `tests/*.test.ts`（node:test，纯逻辑）是两套，各管一段：
 * - `tests/*.test.ts`：纯函数（映射、格式化、边界判断）—— 零 DOM、毫秒级；
 * - `tests/components/*.spec.ts`：组件真的渲染成什么样 —— 要 DOM。
 *
 * 两者都用 `npm test` 一起跑。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/components/**/*.spec.ts"],
    reporters: ["default"],
  },
});
