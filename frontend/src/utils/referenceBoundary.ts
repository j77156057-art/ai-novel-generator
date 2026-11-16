import type { ReferenceCutoff } from "@/api/types";

/**
 * 参考书的两种用法 —— 界面必须让用户**说清是哪一种**。
 *
 * - `style`（只学写法）：参考书只用来学文风，全书都是合法的风格材料；
 *   生成的是**用户自己的故事**。
 * - `continue`（接着这本书写）：生成的内容接在这本书的故事后面。
 *   这时必须逐本给出「故事进行到第几章」—— 模型只会读到该章为止，
 *   后面的章节不进提示词（不剧透，也不把后面的剧情带偏）。
 *
 * 这不是「要不要填一个数字」的差别，而是**两套可见性规则**：
 * 写法参考路径整本可见，续写路径 fail closed（没有边界就整本挡掉）。
 * 界面不问，用户就只能猜；猜大了剧透，猜小了白白挡住可用素材。
 */
export type ReferenceUsage = "style" | "continue";

/**
 * 创作板块 —— 工作台顶部的三条路。
 *
 * ## 为什么是三条而不是两条
 *
 * 「只学写法」与「接着这本书写」是**写正文**的两种用法；「和角色对话」是**另一件事**：
 * 产物是对话记录，不进正文主线。
 *
 * 三条路的**可见性规则**各不相同：
 * - 原创：参考书只提供写法，世界观/角色名一律不可见；
 * - 续写：原文可见，但**只到「故事进行到第 N 章」为止**；
 * - 角色：**只看得见被扮演的那一个角色**，且他只能知道到「此刻」为止的事
 *   （所以它与续写**共用同一条边界**）。
 *
 * 把它们并成一块，用户就不知道模型此刻能看到什么 —— 这个疑问真跑时出现过
 * （「角色聊天不参考原文，那问他只有他们知道的事怎么回答」）。
 */
export type CreationBoard = "original" | "continuation" | "roleplay";

/** 板块 ↔ 参考书用法：角色板块与续写共用「有边界」的那一套。 */
export function boardToUsage(board: CreationBoard): ReferenceUsage {
  return board === "original" ? "style" : "continue";
}

/** 从项目现状推出初始板块（角色板块不持久化 —— 它是界面上的临时选择）。 */
export function boardFromUsage(usage: ReferenceUsage): CreationBoard {
  return usage === "continue" ? "continuation" : "original";
}

/**
 * 每本语料当前填的章号（用户输入，未校验）。
 *
 * 类型上允许字符串：这份数据最终来自 `<input type="number">`，
 * 而输入框的值在「空」与「非数字」时都是字符串 —— 校验要按字符串口径处理，
 * 不能假设它一定是数字（假设错了就会把非法输入当成合法数字存进去）。
 */
export type CutoffInputs = Record<number, number | null | string>;

/** 一本语料能填的章号上界（`max_chapter_no`；0 = 这本书还没有章节）。 */
export type MaxChapterMap = Record<number, number>;

export interface BoundaryDraft {
  /** 故事锚定方式：自己的故事 / 接着参考书写。 */
  story_authority: "generated_branch" | "reference_anchor";
  /** 每本语料的原文边界；null = 没有边界（只学写法）。 */
  reference_cutoffs: ReferenceCutoff[] | null;
  /** 人话错误说明；null = 可以提交。 */
  error: string | null;
}

/**
 * 由界面状态算出要提交什么。
 *
 * 校验放在**提交之前**、且说清是哪本书、哪个数 —— 后端虽然也会拦
 * （「章节不存在」），但那时用户已经填完一整套表单了，回灌成本高得多。
 */
export function buildBoundary(
  usage: ReferenceUsage,
  selectedIds: readonly number[],
  inputs: CutoffInputs,
  maxChapters: MaxChapterMap,
  titleOf: (id: number) => string = (id) => `#${id}`,
): BoundaryDraft {
  const ids = Array.from(new Set(selectedIds));

  if (usage === "style") {
    // 只学写法：明确清掉边界。留着旧边界会**悄悄限制**可用的写法素材
    // （显式边界永远优先于「写法参考路径整本可见」），而用户根本没要求过。
    return { story_authority: "generated_branch", reference_cutoffs: null, error: null };
  }

  if (!ids.length) {
    return {
      story_authority: "reference_anchor",
      reference_cutoffs: null,
      error: "「接着这本书写」需要先选至少一本参考书。",
    };
  }

  const cutoffs: ReferenceCutoff[] = [];
  for (const id of ids) {
    const raw = inputs[id];
    const name = titleOf(id);
    if (raw === null || raw === undefined || raw === "") {
      return {
        story_authority: "reference_anchor",
        reference_cutoffs: null,
        error: `《${name}》还没填「故事进行到第几章」。`,
      };
    }
    const chapter = Number(raw);
    if (!Number.isFinite(chapter)) {
      return {
        story_authority: "reference_anchor",
        reference_cutoffs: null,
        error: `《${name}》的章号必须是数字。`,
      };
    }
    if (!Number.isInteger(chapter) || chapter < 1) {
      return {
        story_authority: "reference_anchor",
        reference_cutoffs: null,
        error: `《${name}》的章号必须是正整数。`,
      };
    }
    const max = maxChapters[id] ?? 0;
    if (max === 0) {
      return {
        story_authority: "reference_anchor",
        reference_cutoffs: null,
        error: `《${name}》还没有切分出章节，无法设置边界。`,
      };
    }
    if (chapter > max) {
      return {
        story_authority: "reference_anchor",
        reference_cutoffs: null,
        error: `《${name}》全书到第 ${max} 章，第 ${chapter} 章不存在。`,
      };
    }
    cutoffs.push({ novel_id: id, chapter });
  }
  return { story_authority: "reference_anchor", reference_cutoffs: cutoffs, error: null };
}

/**
 * 从项目现状还原界面状态。
 *
 * 项目里存了 `story_authority` 与 `reference_cutoffs`，界面要能**回显**它们 ——
 * 打开一个已经设好边界的项目，看到的应该是「接着这本书写 · 读到第 N 章」，
 * 而不是默认的「只学写法」。
 */
export function boundaryFromProject(project: {
  story_authority?: string | null;
  reference_cutoffs?: ReferenceCutoff[] | null;
} | null): { usage: ReferenceUsage; inputs: CutoffInputs } {
  const inputs: CutoffInputs = {};
  const cutoffs = project?.reference_cutoffs ?? null;
  if (Array.isArray(cutoffs)) {
    for (const item of cutoffs) {
      if (item && typeof item.novel_id === "number" && typeof item.chapter === "number") {
        inputs[item.novel_id] = item.chapter;
      }
    }
  }
  const anchored = project?.story_authority === "reference_anchor";
  // 有边界就一定是「接着写」；反之，锚定方式说了算（锚定了却没边界，
  // 界面如实显示成「接着写」并把边界标成未填，而不是替用户改成「只学写法」）。
  const usage: ReferenceUsage = anchored || Object.keys(inputs).length > 0 ? "continue" : "style";
  return { usage, inputs };
}

/**
 * 一句话说明当前边界状态（给界面的标签用）。
 *
 * 界面上要看得见「这次到底读到哪」—— 边界藏在表单里，生成页就没人知道
 * 为什么模型读不到后面的内容。
 */
export function boundaryLabel(
  usage: ReferenceUsage,
  inputs: CutoffInputs,
  titleOf: (id: number) => string = (id) => `#${id}`,
): string {
  if (usage === "style") return "只学写法（全书可作写法材料）";
  const parts = Object.keys(inputs)
    .map((k) => Number(k))
    .filter((id) => {
      const v = inputs[id];
      return v !== null && v !== undefined && v !== "";
    })
    .sort((a, b) => a - b)
    .map((id) => `《${titleOf(id)}》第 ${inputs[id]} 章`);
  return parts.length ? `接着写 · ${parts.join("、")}` : "接着写（边界未填）";
}
