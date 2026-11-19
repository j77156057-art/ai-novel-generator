import { afterEach } from "vitest";

/**
 * happy-dom 已实现大部分浏览器 API；这里只补工作台挂载需要、
 * 但它在当前版本尚未提供或行为不完整的少数接口。
 */

type ResizeObserverLike = {
  observe(): void;
  unobserve(): void;
  disconnect(): void;
};

const globalAny = globalThis as unknown as {
  ResizeObserver?: new (callback: () => void) => ResizeObserverLike;
};

if (!globalAny.ResizeObserver) {
  globalAny.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  document.body.innerHTML = "";
  localStorage.clear();
});
