/**
 * 结构检查回执：把后端的 `structure_check` 翻成三条可见的状态行。
 *
 * 纪律：**文案由后端数据驱动**。跳过项的正文直接使用后端 `reason.message`，
 * 这里不写「段模式不跑」这类静态判断 —— 否则等哪天检查真跑起来，
 * 界面还会留着那句旧话，变成新的假状态。
 */

/** 三项固定顺序：与后端 `check_structure` 的返回键一致。 */
export const STRUCTURE_CHECK_KEYS = ["beat", "premature", "gender"];

const CHECK_LABELS = {
  beat: "情节点覆盖",
  premature: "越章材料",
  gender: "人物性别一致性",
};

/** 跳过原因的短标签，只用于标题右侧的 tag；原因正文仍取后端 message。 */
const REASON_LABELS = {
  no_text: "正文为空",
  no_chapter_label: "无章号",
  chapter_beyond_outline: "超出章纲条数",
  outline_entry_empty: "章纲条目为空",
  no_character_cards: "没有人物卡",
  disabled: "检查已关闭",
};

function checkedBeat(item) {
  const fallback = item.fallback && typeof item.fallback === "object" ? item.fallback : null;
  if (item.basis === "fallback_term_coverage" && fallback) {
    // 后端字段是 `coverage`（不是 ratio），见 `_beat_fallback` 的返回。
    const ratio = Number(fallback.coverage);
    const pct = Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : "—";
    const passed = Boolean(fallback.passed);
    return {
      tone: passed ? "ok" : "warn",
      text: `已检查 · 章纲条目不足 3 条，改用兜底词项覆盖 ${pct}（${passed ? "通过" : "未通过"}）`,
    };
  }
  const total = Number(item.total);
  const covered = Number(item.covered);
  const missing = Array.isArray(item.missing) ? item.missing : [];
  const head = Number.isFinite(total) && Number.isFinite(covered)
    ? `已检查 · 覆盖 ${covered}/${total} 个情节点`
    : "已检查";
  return {
    tone: missing.length ? "warn" : "ok",
    text: missing.length ? `${head}，漏掉：${missing.join("；")}` : head,
  };
}

function checkedPremature(item) {
  const chapters = Number(item.chapters_hit);
  const max = Number(item.max_hits);
  const ok = Number.isFinite(chapters) && Number.isFinite(max) ? chapters <= max : true;
  const terms = Array.isArray(item.hits)
    ? item.hits.reduce((n, h) => n + (Array.isArray(h?.terms) ? h.terms.length : 0), 0)
    : 0;
  // 后端口径：`max_hits` 是比较「命中了几个后续章」的上限，单位是章不是处。
  return {
    tone: ok ? "ok" : "warn",
    text: ok
      ? `已检查 · 未超过越章上限（命中 ${chapters} 章 / ${terms} 个词项，上限 ${max} 章）`
      : `已检查 · 命中 ${chapters} 章 / ${terms} 个词项，超过上限 ${max} 章`,
  };
}

function checkedGender(item) {
  const count = Number(item.count);
  const max = Number(item.max_mismatches);
  const ok = Number.isFinite(count) && Number.isFinite(max) ? count <= max : true;
  return {
    tone: ok ? "ok" : "warn",
    text: `已检查 · ${Number.isFinite(count) ? count : "—"} 处性别不一致（上限 ${Number.isFinite(max) ? max : "—"}）`,
  };
}

function checkedText(key, item) {
  if (key === "beat") return checkedBeat(item);
  if (key === "premature") return checkedPremature(item);
  return checkedGender(item);
}

/**
 * 生成三条状态行；没有 `structure_check`（旧数据 / 旧后端）时返回空数组，
 * 由界面决定不渲染 —— 不编造原因，也不写死「段模式不跑」。
 *
 * @param {unknown} structureCheck
 * @returns {{key: string, label: string, status: string, statusLabel: string,
 *            tone: "ok"|"warn"|"idle", code: string, text: string}[]}
 */
export function buildStructureNotices(structureCheck) {
  if (!structureCheck || typeof structureCheck !== "object") return [];
  return STRUCTURE_CHECK_KEYS.map((key) => {
    const label = CHECK_LABELS[key] || key;
    const raw = structureCheck[key];
    if (!raw || typeof raw !== "object") {
      return {
        key,
        label,
        status: "missing",
        statusLabel: "无结果",
        tone: "warn",
        code: "missing_result",
        text: "后端没有返回这一项的检查结果。",
      };
    }
    const item = raw;
    if (item.status === "checked") {
      const view = checkedText(key, item);
      return {
        key,
        label,
        status: "checked",
        statusLabel: "已检查",
        tone: view.tone,
        code: "checked",
        text: view.text,
      };
    }
    if (item.status === "skipped") {
      const reason = item.reason && typeof item.reason === "object" ? item.reason : {};
      const code = String(reason.code || "missing_reason");
      const message = String(reason.message || "").trim();
      return {
        key,
        label,
        status: "skipped",
        statusLabel: REASON_LABELS[code] || "跳过",
        tone: "idle",
        code,
        text: message || "后端标记为跳过，但没有给出原因。",
      };
    }
    return {
      key,
      label,
      status: String(item.status || "unknown"),
      statusLabel: "未识别",
      tone: "warn",
      code: "unknown_status",
      text: `后端返回了未识别的检查状态：${String(item.status || "（空）")}`,
    };
  });
}
