/** 本轮数据块（chip / 明细小窗口）共用的类型。 */

export type DataChipKind = "steps" | "evidence" | "tools" | "web";

export interface DataChip {
  kind: DataChipKind;
  label: string;
  live: boolean;
}

/** 明细窗口里的「读取步骤」：`meta/detail` 可能为空（实时步骤只有名字）。 */
export interface DetailStep {
  name: string;
  meta?: string;
  detail?: string;
}
