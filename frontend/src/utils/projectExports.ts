import type { CharacterCardItem, ProjectOut, Reminders } from "@/api/types";

export const PROJECT_EXPORT_TYPES = ["outline", "characters", "reminders"] as const;

export type ProjectExportType = (typeof PROJECT_EXPORT_TYPES)[number];

/** 文件名、导出内容与界面共用同一套类型名称。 */
export const PROJECT_EXPORT_TYPE_LABELS: Record<ProjectExportType, string> = {
  outline: "大纲",
  characters: "人物表",
  reminders: "坑清单",
};

export function projectExportTypeLabel(type: ProjectExportType): string {
  return PROJECT_EXPORT_TYPE_LABELS[type];
}

/** 去掉 Windows / macOS 不能作为文件名的字符，不限制中文等 Unicode 字符。 */
export function sanitizeFilename(value: string): string {
  return value.replace(/[<>:"\/\\|?*\u0000-\u001F\u007F]/g, "").trim();
}

export function projectExportFilename(
  project: Pick<ProjectOut, "title">,
  type: ProjectExportType,
): string {
  const title = sanitizeFilename(project.title) || "未命名作品";
  return `${title}-${projectExportTypeLabel(type)}.md`;
}

/** 数组大纲转成有序列表；字符串大纲保留其中的 Markdown 结构。 */
export function formatOutlineMarkdown(outline: ProjectOut["outline"]): string {
  if (typeof outline === "string") {
    const text = outline.trim();
    return `# 大纲\n\n${text || "暂无大纲。"}\n`;
  }
  const items = (outline ?? []).map((item) => item.trim()).filter(Boolean);
  if (!items.length) return "# 大纲\n\n暂无大纲。\n";
  return `# 大纲\n\n${items.map((item, index) => `${index + 1}. ${item}`).join("\n")}\n`;
}

/** 把角色卡里的数组、嵌套对象写成可读文本，避免出现「[object Object]」。 */
function readableValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value.replace(/\s+/g, " ").trim();
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.map(readableValue).filter(Boolean).join("；");
  }
  if (typeof value === "object") {
    return Object.entries(value)
      .map(([key, nested]) => `${key}：${readableValue(nested) || "未填写"}`)
      .join("，");
  }
  return String(value);
}

function appendCardSection(lines: string[], title: string, value: unknown): void {
  if (value === null || value === undefined) return;
  if (typeof value === "string") {
    const text = readableValue(value);
    if (text) lines.push("", `### ${title}`, "", text);
    return;
  }
  if (typeof value !== "object" || Array.isArray(value)) return;

  const entries = Object.entries(value);
  if (!entries.length) return;
  lines.push("", `### ${title}`, "");
  for (const [key, nested] of entries) {
    lines.push(`- ${key}：${readableValue(nested) || "未填写"}`);
  }
}

export function formatCharacterCardsMarkdown(characters: CharacterCardItem[]): string {
  if (!characters.length) return "# 人物表\n\n暂无人物表。\n";

  const lines = ["# 人物表"];
  for (const character of characters) {
    lines.push(
      "",
      `## ${readableValue(character.name) || "未命名人物"}`,
      "",
      `- 角色：${readableValue(character.role) || "未填写"}`,
      `- 动机：${readableValue(character.motivation) || "未填写"}`,
      `- 卡片来源：${readableValue(character.card_source) || "未填写"}`,
      `- 个性来源：${readableValue(character.persona_source) || "未填写"}`,
    );
    appendCardSection(lines, "个性", character.persona);
    appendCardSection(lines, "身份", character.card.身份);
    appendCardSection(lines, "能力", character.card.能力);
    appendCardSection(lines, "声纹", character.card.声纹);
    appendCardSection(lines, "关系", character.card.关系);
    appendCardSection(lines, "来源", character.card.来源);
  }
  return `${lines.join("\n")}\n`;
}

function chapterLabel(chapter: number | null): string {
  return chapter === null ? "章号未知" : `第 ${chapter} 章`;
}

export function formatRemindersMarkdown(reminders: Reminders): string {
  const lines = ["# 坑清单", "", "## 未解伏笔", ""];
  if (!reminders.unresolved_plotholes.length) {
    lines.push("暂无未解伏笔。");
  } else {
    for (const plothole of reminders.unresolved_plotholes) {
      const question = readableValue(plothole.question) || "未填写问题";
      lines.push(`- ${question}（首次提及：${chapterLabel(plothole.first_chapter)}）`);
    }
  }

  lines.push("", "## 人物约束", "");
  if (!reminders.character_constraints.length) {
    lines.push("暂无人物约束。");
  } else {
    for (const constraint of reminders.character_constraints) {
      const name = readableValue(constraint.name) || "未命名人物";
      const mentions = constraint.mentions === null ? "次数未知" : `${constraint.mentions} 次`;
      lines.push(
        `- ${name}（提及 ${mentions}；首次提及：${chapterLabel(constraint.first_chapter)}；最近提及：${chapterLabel(constraint.last_chapter)}）`,
      );
    }
  }
  return `${lines.join("\n")}\n`;
}

/** 仅在浏览器 DOM 可用时调用；调用方负责提供已经清理过的文件名。 */
export function downloadTextFile(filename: string, content: string): void {
  if (typeof document === "undefined" || typeof URL.createObjectURL !== "function") return;
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
