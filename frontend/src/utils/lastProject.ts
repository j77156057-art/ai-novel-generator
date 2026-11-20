/**
 * 「最后打开的作品」记忆。
 *
 * 为什么需要：全局侧栏的「写作台」在列表页 / 设置页这类没有项目 id 的
 * 页面上只能跳到 `/assistant`，而 `/assistant` 会自动挑一个项目。原先它
 * 优先挑「挂了参考语料的项目」，于是用户在写 A 书时去了一趟设置页，再点
 * 写作台就落到了 B 书——记忆、会话、上下文全跟着换了，看起来像「数据丢了」。
 *
 * 只存一个数字 id，读写失败都静默：它是导航便利，不是数据源。
 */
const KEY = "app:last-project";

export function rememberProject(id: number): void {
  if (!Number.isInteger(id) || id <= 0) return;
  try {
    localStorage.setItem(KEY, String(id));
  } catch {
    /* 隐私模式 / 配额满：只影响下次默认落点 */
  }
}

export function lastProjectId(): number | null {
  try {
    const raw = localStorage.getItem(KEY);
    const id = Number(raw);
    return Number.isInteger(id) && id > 0 ? id : null;
  } catch {
    return null;
  }
}
