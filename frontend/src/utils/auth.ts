import { computed, ref } from "vue";

const STORAGE_KEY = "ai-novel-generator.username";

function readUsername(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(STORAGE_KEY)?.trim() ?? "";
}

export const username = ref(readUsername());
export const isLoggedIn = computed(() => username.value.trim().length > 0);

export function login(value: string): boolean {
  const nextUsername = value.trim();
  if (!nextUsername) return false;

  username.value = nextUsername;
  window.localStorage.setItem(STORAGE_KEY, nextUsername);
  return true;
}

export function logout(): void {
  username.value = "";
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(STORAGE_KEY);
  }
}
