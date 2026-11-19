import { describe, expect, it } from "vitest";
import { defineComponent, h, ref } from "vue";
import { mount } from "@vue/test-utils";

import { useModalResize } from "@/utils/useModalResize";

/**
 * 模态弹窗拖拽缩放的纯逻辑测试。
 *
 * 用 render 函数而不是模板字符串：运行时版 Vue 不带模板编译器，
 * 用 `h()` 在测试里更稳，也不依赖 vitest 额外 alias。
 */

type ModalApi = ReturnType<typeof useModalResize>;

let modal: ModalApi;

const Host = defineComponent({
  setup() {
    const el = ref<HTMLElement | null>(null);
    modal = useModalResize(el);
    return () =>
      h("div", { ref: el, "data-test": "box", style: modal.style.value }, [
        h("span", { "data-test": "handle", onPointerdown: modal.start }),
      ]);
  },
});

function mountHost() {
  // 必须挂到 document.body：composable 内部用的是真实 DOM 元素尺寸与 window 事件。
  return mount(Host, { attachTo: document.body });
}

function stubRect(width = 640, height = 480): HTMLElement {
  const box = document.querySelector('[data-test="box"]') as HTMLElement;
  box.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: width,
      bottom: height,
      width,
      height,
      toJSON: () => ({}),
    }) as DOMRect;
  return box;
}

function pointerDown(x: number, y: number): void {
  const handle = document.querySelector('[data-test="handle"]') as HTMLElement;
  handle.dispatchEvent(
    new MouseEvent("pointerdown", {
      clientX: x,
      clientY: y,
      bubbles: true,
      cancelable: true,
    }),
  );
}

function pointerMove(x: number, y: number): void {
  window.dispatchEvent(
    new MouseEvent("pointermove", { clientX: x, clientY: y, bubbles: true }),
  );
}

function pointerUp(): void {
  window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true }));
}

describe("useModalResize", () => {
  it("拖动后按位移放大，并在 pointerup 后停止跟随", () => {
    const wrapper = mountHost();
    stubRect();
    expect(modal.style.value).toEqual({});

    pointerDown(100, 100);
    pointerMove(260, 220);
    expect(modal.style.value).toMatchObject({
      width: "800px",
      height: "600px",
      maxWidth: "none",
      maxHeight: "none",
    });

    pointerUp();
    pointerMove(900, 900);
    // 松手后的 move 不再生效。
    expect(modal.style.value).toMatchObject({ width: "800px", height: "600px" });
    wrapper.unmount();
  });

  it("向左上拖过头时钳到最小尺寸 360×320", () => {
    const wrapper = mountHost();
    stubRect();

    pointerDown(600, 500);
    pointerMove(-1000, -1000);
    expect(modal.style.value).toMatchObject({ width: "360px", height: "320px" });
    pointerUp();
    wrapper.unmount();
  });

  it("reset 恢复默认尺寸（清空显式 style）", () => {
    const wrapper = mountHost();
    stubRect();

    pointerDown(100, 100);
    pointerMove(200, 200);
    pointerUp();
    expect(modal.style.value).not.toEqual({});

    modal.reset();
    expect(modal.style.value).toEqual({});
    wrapper.unmount();
  });
});
