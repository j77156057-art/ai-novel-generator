/**
 * 纯量层读数：汉字 / 标点 / 空白 + 占比。
 *
 * ## 为什么界面上要自己数一遍
 *
 * 界面原来显示的是「X 字」，取的是 JS 的 `text.length` ——
 * **把标点和换行一起数进去了**。于是同一段正文，说「1200 字」时读者脑子里想的是
 * 「1200 个汉字」，实际可能是 1050 汉字 + 130 标点 + 20 换行。
 * **这个差就是 15%**，而 15% 恰好能把一段「达标」的文字显示成「差 150 字」。
 *
 * 更要紧的是：标点占比与空白占比**就是作者的写法**（用多少标点、多久换段），
 * 后端 `features()` 里已经把它们单列成特征，界面上却一点都看不见 ——
 * 特征在后台跑，用户在界面上找不到对应的东西。
 *
 * ## ⚠️ 口径必须与后端一致
 *
 * 本文件的集合必须与 `backend/app/style/stylometry.py` 的
 * `_CJK_CHAR` / `_PUNCT_ALL` / `raw_counts` **逐字一致**。
 * 两边不一致 = 界面显示的汉字数与后端段长闸门判的汉字数不是一回事 ——
 * 那正是「界面说达标、闸门说没达标」这类鬼故事的来源。
 *
 * 改一边必须改另一边：`frontend/tests/measure.test.ts`（本文件）
 * 与 `tests/test_measure_layers.py`（后端）各有一份口径用例。
 */

/** 汉字：U+4E00–U+9FFF（与 `stylometry._CJK_CHAR` 同）。 */
const CJK_RE = /[\u4e00-\u9fff]/g;

/**
 * 中文标点全集：与 `stylometry._PUNCT_ALL` 同。
 * 顺序无所谓，集合一致即可（两边都是「按出现次数累加」）。
 */
const PUNCT_RE = /[，。！？…、；：—·「」『』“”‘’（）《》〈〉【】〔〕]/g;

/**
 * 空白：与 Python `str.isspace()` 的集合**逐字一致**。
 *
 * 不能直接用 JS 的 `\s`：它多了 U+FEFF（Python 不算空白），
 * 又少了 U+0085 与 U+001C–U+001F（Python 算空白）。
 * 差一个字节就差一个读数，而读数是要跟后端对账的。
 */
const WS_RE = /[\t\n\v\f\r\x1c-\x1f\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/g;

/** 一段文本的纯量层读数（与后端 `stylometry.measure_summary` 同形）。 */
export interface Measure {
  /** 字符数（**码点**数，与 Python `len(text)` 一致，不是 UTF-16 长度）。 */
  chars: number;
  /** 汉字数。段长闸门量的就是这个。 */
  cjk: number;
  /** 标点数。 */
  punct: number;
  /** 空白数（换行、空格、全角空格…）。 */
  blank: number;
  /** 标点占字符数的百分比。 */
  punctPct: number;
  /** 空白占字符数的百分比。 */
  blankPct: number;
}

/**
 * 后端 `measure_summary` 下发的**原始形状**（snake_case）。
 *
 * 后端是 Python，字段名用 snake_case 是那边的规矩；
 * 前端一律转成 camelCase 再用（`toMeasure`），
 * 免得同一个量在代码里两种写法 —— 那种「两种写法」最后总会写错一处。
 */
export interface RawMeasure {
  chars: number;
  cjk: number;
  punct: number;
  blank: number;
  paras?: number;
  sents?: number;
  punct_pct: number;
  blank_pct: number;
}

/** 把后端的读数转成界面用的形状（snake_case → camelCase）。 */
export function toMeasure(raw: RawMeasure | null | undefined): Measure | null {
  if (!raw) return null;
  return {
    chars: raw.chars,
    cjk: raw.cjk,
    punct: raw.punct,
    blank: raw.blank,
    punctPct: raw.punct_pct,
    blankPct: raw.blank_pct,
  };
}

/** 四舍五入到两位小数（与后端 `round(x, 2)` 同）。 */
function round2(v: number): number {
  return Math.round(v * 100) / 100;
}

/** 数一段文本的汉字 / 标点 / 空白。 */
export function measureText(text: string): Measure {
  const t = text || "";
  const chars = Array.from(t).length; // 码点数，对齐 Python 的 len()
  const cjk = (t.match(CJK_RE) || []).length;
  const punct = (t.match(PUNCT_RE) || []).length;
  const blank = (t.match(WS_RE) || []).length;
  return {
    chars,
    cjk,
    punct,
    blank,
    punctPct: chars ? round2((punct / chars) * 100) : 0,
    blankPct: chars ? round2((blank / chars) * 100) : 0,
  };
}

/**
 * 展示成一行：`汉字 1,052 · 标点 12.4% · 空白 1.8%`。
 *
 * 「汉字」在前，是因为它才是段长闸门量的那个数；
 * 后两个是**写法**（作者用多少标点、多久换段），属另两码事，不混在「字数」里。
 */
export function formatMeasure(m: Measure | null | undefined): string {
  if (!m) return "";
  return `汉字 ${m.cjk.toLocaleString()} · 标点 ${m.punctPct.toFixed(1)}% · 空白 ${m.blankPct.toFixed(1)}%`;
}
