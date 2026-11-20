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
    // KG v3.0 工作台：9 表读侧可视化（切片/情报/时间线/Judge/关系）。
    // 项目级资源，与助手工作台同逻辑挂在 /projects/:id 之下。
    path: "/projects/:id/kg",
    name: "kg-workspace",
    component: () => import("@/views/KgWorkspace.vue"),
    meta: { title: "KG 工作台", idx: "02" },
  },
  {
    // 章纲写入/编辑：逐章状态（已确认/缺失/超出末章）、校验回执、AI 起草。
    // 独立页而不是塞进工作台：章纲是长列表，编辑与生成正文的节奏也不同。
    path: "/projects/:id/outline",
    name: "chapter-outline",
    component: () => import("@/views/ChapterOutlineView.vue"),
    meta: { title: "章纲", idx: "02" },
  },
  {
    // 全书摘要：书级产物（跨项目复用），提交挂在项目下（后台任务表项目列必填）。
    path: "/projects/:id/digest",
    name: "book-digest",
    component: () => import("@/views/BookDigestView.vue"),
    meta: { title: "全书摘要", idx: "02" },
  },
  {
    // 人物塑造模板：库里按档位存多份，应用后才进入本项目生成提示。
    path: "/projects/:id/templates",
    name: "character-template",
    component: () => import("@/views/CharacterTemplateView.vue"),
    meta: { title: "人物模板", idx: "02" },
  },
  {
    // 角色卡：身份 / 能力 / 声纹 / 关系的静态档案（与角色状态分开）。
    path: "/projects/:id/cards",
    name: "character-cards",
    component: () => import("@/views/CharacterCardsView.vue"),
    meta: { title: "角色卡", idx: "02" },
  },
  {
    // 力量体系：修炼体系 + 档位分层 + 角色修炼档案。
    path: "/projects/:id/power",
    name: "power-system",
    component: () => import("@/views/PowerSystemView.vue"),
    meta: { title: "力量体系", idx: "02" },
  },
  {
    // 项目资产：摘要 / 人物模板 / 角色卡 / 力量体系的统一入口，
    // 不再把四个按钮直接堆在项目工作台头部。
    path: "/projects/:id/assets",
    name: "project-assets",
    component: () => import("@/views/ProjectAssetsView.vue"),
    meta: { title: "项目资产", idx: "02" },
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
    // 「我的小说」与「创作项目」是同一份书单，已合并到 /projects；
    // 旧链接直接重定向，避免书签 404。
    path: "/mine",
    redirect: "/projects",
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
