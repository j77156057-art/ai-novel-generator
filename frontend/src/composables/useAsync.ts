import { ref } from "vue";

/**
 * 极简异步状态：data / loading / error + run()。
 * 调用处解构使用：const { data, loading, error, run } = useAsync(...)
 * （解构后顶层 ref 在模板中自动解包。）
 */
export function useAsync<T>(fn: () => Promise<T>) {
  const data = ref<T | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  async function run() {
    loading.value = true;
    error.value = null;
    try {
      data.value = await fn();
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, run };
}
