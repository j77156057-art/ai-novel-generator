import { createRouter, createWebHistory } from "vue-router";

const routes = [
  // 打开应用直接进入助手对话：/assistant 会自动挑一个项目落到它的助手页，
  // 不再先弹项目列表（产品决策：助手是对话框，打开即聊）。
  { path: "/", redirect: "/assistant" },
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
    // 顶层助手入口：自动挑一个项目后 replace 到上面的项目级助手页。
    // 让「助手工作台」导航与首屏都能直达对话，不必先经项目列表。
    path: "/assistant",
    name: "assistant-root",
    component: () => import("@/views/AssistantResolver.vue"),
    meta: { title: "助手工作台", idx: "05" },
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
