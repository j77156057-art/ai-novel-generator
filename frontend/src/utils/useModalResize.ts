import { computed, onBeforeUnmount, ref, type Ref } from "vue";

type Size = { width: number; height: number };

const MIN_WIDTH = 360;
const MIN_HEIGHT = 320;
const MARGIN = 24;

/**
 * 模态卡片的右下角拖拽缩放。
 *
 * 只维护「这次拖拽后的显式尺寸」：未拖过时返回空 style，让弹窗沿用 CSS
 * 里的默认宽度/高度；拖过之后才写死 px，并解除 max-width/max-height 限制。
 */
export function useModalResize(elRef: Ref<HTMLElement | null>) {
  const size = ref<Size | null>(null);
  const resizing = ref(false);
  let startX = 0;
  let startY = 0;
  let startWidth = 0;
  let startHeight = 0;

  function reset() {
    size.value = null;
  }

  function onMove(event: PointerEvent) {
    if (!resizing.value) return;
    const maxWidth = window.innerWidth - MARGIN * 2;
    const maxHeight = window.innerHeight - MARGIN * 2;
    size.value = {
      width: Math.min(
        Math.max(startWidth + event.clientX - startX, MIN_WIDTH),
        maxWidth,
      ),
      height: Math.min(
        Math.max(startHeight + event.clientY - startY, MIN_HEIGHT),
        maxHeight,
      ),
    };
  }

  function stop() {
    if (!resizing.value) return;
    resizing.value = false;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", stop);
    document.body.classList.remove("modal-resizing");
  }

  function start(event: PointerEvent) {
    const element = elRef.value;
    if (!element) return;
    event.preventDefault();
    const rect = element.getBoundingClientRect();
    startX = event.clientX;
    startY = event.clientY;
    startWidth = rect.width;
    startHeight = rect.height;
    size.value = { width: rect.width, height: rect.height };
    resizing.value = true;
    document.body.classList.add("modal-resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
  }

  onBeforeUnmount(stop);

  const style = computed(() =>
    size.value
      ? {
          width: `${size.value.width}px`,
          height: `${size.value.height}px`,
          maxWidth: "none",
          maxHeight: "none",
        }
      : {},
  );

  return { size, resizing, style, start, stop, reset };
}
