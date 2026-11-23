/**
 * 把任意异常翻成可直接展示的一句中文（全站唯一口径）。
 *
 * 此前 10 个视图各写一份 `textOf` / `errorText`，口径已经分叉：只有
 * ProjectsView / ProjectWorkspace 会把 `fetch failed` 翻成「无法连接服务」，
 * 其余页面直接甩英文 `Failed to fetch`；ApiError 的结构化 detail 也只有
 * 助手页会用。统一到这里后，后端 4xx/5xx 的 message、网络断开的提示、
 * 普通 Error 的文案在哪个页面都一致。
 */
import { ApiError } from "@/api/client";

export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    // 后端常见两种 detail：`{error, message, hint}` 对象，或直接一句字符串。
    const detail = err.detail;
    if (detail && typeof detail === "object") {
      const message = (detail as Record<string, unknown>).message;
      if (typeof message === "string" && message) return message;
    }
    if (typeof detail === "string" && detail) return detail;
    return err.message;
  }
  // 后端没起来时浏览器抛的是 TypeError: Failed to fetch（各浏览器文案不同）。
  if (err instanceof TypeError && /fetch/i.test(err.message)) {
    return "无法连接服务，请检查网络连接或后端启动状态。";
  }
  return err instanceof Error ? err.message : String(err);
}
