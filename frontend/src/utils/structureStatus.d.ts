/** 结构检查回执的类型声明（实现见 structureStatus.js）。 */
export type StructureNoticeTone = "ok" | "warn" | "idle";

export interface StructureNotice {
  key: string;
  label: string;
  status: string;
  statusLabel: string;
  tone: StructureNoticeTone;
  code: string;
  text: string;
}

export function buildStructureNotices(structureCheck: unknown): StructureNotice[];
