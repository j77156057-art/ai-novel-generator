<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { login } from "@/utils/auth";

const route = useRoute();
const router = useRouter();
const name = ref("");
const error = ref("");

function safeRedirect(value: unknown): string {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    ? value
    : "/assistant";
}

async function submit() {
  if (!login(name.value)) {
    error.value = "请输入用户名。";
    return;
  }

  error.value = "";
  await router.replace(safeRedirect(route.query.redirect));
}
</script>

<template>
  <div class="login-page">
    <section class="login-intro" aria-labelledby="login-title">
      <div class="eyebrow">LOCAL WORKSPACE · 00</div>
      <h1 id="login-title">继续你的写作。</h1>
      <p>进入个人工作区，保留项目、语料与创作讨论的同一条工作脉络。</p>
      <div class="edition-mark" aria-label="本地编辑工作区">
        <span>NOVEL STUDIO</span>
        <span>LOCAL EDITION</span>
      </div>
    </section>

    <section class="login-panel" aria-label="登录">
      <div class="panel-heading">
        <span class="panel-index">01 / 身份</span>
        <h2>进入工作台</h2>
        <p>用一个用户名标识当前写作会话。</p>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <div class="field">
          <label for="login-username">用户名</label>
          <input
            id="login-username"
            v-model="name"
            class="input login-input"
            type="text"
            autocomplete="username"
            placeholder="你的名字或笔名"
            required
            autofocus
            @input="error = ''"
          />
        </div>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button class="btn primary login-submit" type="submit">
          进入工作台 <span aria-hidden="true">→</span>
        </button>
      </form>

      <div class="local-note">
        <span class="local-dot" aria-hidden="true"></span>
        <div>
          <strong>本地单用户占位，不验证密码</strong>
          <p>用户名仅保存在此浏览器，用于标识当前会话。</p>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.login-page {
  width: min(100%, 920px);
  margin: clamp(20px, 7vh, 72px) auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 400px);
  gap: clamp(40px, 7vw, 88px);
  align-items: center;
}

.login-intro {
  padding: 8px 0;
}

.eyebrow,
.panel-index,
.edition-mark {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  letter-spacing: 0.09em;
}

.eyebrow {
  color: var(--accent-ink);
}

.login-intro h1 {
  margin-top: 14px;
  font-family: var(--font-serif);
  font-size: clamp(32px, 4vw, 44px);
  font-weight: 700;
  letter-spacing: -0.03em;
}

.login-intro > p {
  max-width: 30em;
  margin: 18px 0 0;
  color: var(--fg-muted);
  line-height: 1.9;
}

.edition-mark {
  margin-top: 44px;
  padding-top: 14px;
  border-top: var(--border-w) solid var(--border-strong);
  display: flex;
  justify-content: space-between;
  gap: var(--gap);
  color: var(--fg-faint);
}

.login-panel {
  padding: clamp(24px, 4vw, 36px);
  background: var(--bg-elev);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
}

.panel-index {
  color: var(--fg-faint);
}

.panel-heading h2 {
  margin-top: 10px;
  font-family: var(--font-serif);
  font-size: var(--fs-xl);
}

.panel-heading p {
  margin: 8px 0 0;
  color: var(--fg-muted);
  font-size: var(--fs-sm);
}

.login-form {
  margin-top: 30px;
}

.login-input {
  width: 100%;
  min-height: 44px;
  padding: 10px 12px;
}

.login-submit {
  width: 100%;
  min-height: 44px;
  margin-top: 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.form-error {
  margin: 8px 0 -8px;
  color: var(--danger);
  font-size: var(--fs-xs);
}

.local-note {
  margin-top: 28px;
  padding-top: 20px;
  border-top: var(--border-w) solid var(--border);
  display: flex;
  align-items: flex-start;
  gap: 10px;
  color: var(--fg-muted);
  font-size: var(--fs-xs);
}

.local-note strong {
  color: var(--fg);
  font-weight: 600;
}

.local-note p {
  margin: 4px 0 0;
  line-height: 1.7;
}

.local-dot {
  width: 7px;
  height: 7px;
  margin-top: 6px;
  flex: none;
  border-radius: var(--radius-full);
  background: var(--accent);
}

@media (max-width: 720px) {
  .login-page {
    margin-top: 12px;
    grid-template-columns: 1fr;
    gap: 28px;
  }

  .edition-mark {
    margin-top: 26px;
  }
}
</style>
