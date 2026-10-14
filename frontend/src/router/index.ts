import { createRouter, createWebHistory } from "vue-router";

const routes = [
  { path: "/", redirect: "/corpus" },
  {
    path: "/corpus",
    name: "corpus",
    component: () => import("@/views/CorpusView.vue"),
    meta: { title: "语料库", idx: "01" },
  },
  {
    path: "/projects",
    name: "projects",
    component: () => import("@/views/ProjectsView.vue"),
    meta: { title: "创作项目", idx: "02" },
  },
  {
    path: "/projects/:id",
    name: "workspace",
    component: () => import("@/views/ProjectWorkspace.vue"),
    meta: { title: "项目工作台", idx: "02" },
  },
  {
    // 助手工作台是**项目级**资源，所以挂在 /projects/:id 之下而不是顶层
    // /assistant/:projectId：与既有的 /projects/:id 保持同一参数名（id），
    // 项目相关 URL 聚在一处，也不会和未来顶层路由抢命名空间。
    path: "/projects/:id/assistant",
    name: "assistant",
    component: () => import("@/views/AssistantWorkspace.vue"),
    meta: { title: "助手工作台", idx: "02" },
  },
  {
    path: "/mine",
    name: "mine",
    component: () => import("@/views/MyNovels.vue"),
    meta: { title: "我的小说", idx: "03" },
  },
  {
    path: "/settings",
    name: "settings",
    component: () => import("@/views/LlmSettings.vue"),
    meta: { title: "模型设置", idx: "04" },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});
