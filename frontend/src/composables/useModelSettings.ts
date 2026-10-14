import { ref } from "vue";

/**
 * 双模型设置弹窗的全局开关（模块级单例 ref）。
 *
 * 弹窗本体只在 App.vue 挂载一份；侧边导航、助手页、旧模型设置页都通过
 * 这个组合式函数打开同一个实例，避免多处各挂一份弹窗、状态互相打架。
 */
const open = ref(false);

export function useModelSettingsDialog() {
  return {
    open,
    openDialog: () => {
      open.value = true;
    },
    closeDialog: () => {
      open.value = false;
    },
  };
}
