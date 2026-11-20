/**
 * 极简 Markdown 渲染（助手回答专用）。
 *
 * 为什么不引第三方库：这里只需要「标题 / 粗体 / 列表 / 引用 / 代码 / 表格 /
 * 分隔线」，模型输出也基本只用这些；自己实现可以把**转义放在第一步**，
 * 保证 `v-html` 渲染的内容不会把模型输出里的 HTML 当结构执行。
 *
 * 输入永远是模型返回的纯文本：先把 `<>&"'` 全部转义，再做标记替换；
 * 因此这里生成的结构标签是唯一的 HTML 来源。
 */

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      default:
        return "&#39;";
    }
  });
}

function inline(text: string): string {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  return out;
}

/** 渲染一段 Markdown 为可安全交给 v-html 的 HTML。 */
export function renderMarkdown(src: string): string {
  const lines = String(src ?? "").split("\n");
  const out: string[] = [];
  let listType = "";
  let inCode = false;
  let code: string[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push("<p>" + inline(paragraph.join(" ")) + "</p>");
      paragraph = [];
    }
  };
  const closeList = () => {
    if (listType) {
      out.push("</" + listType + ">");
      listType = "";
    }
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    if (line.trim().startsWith("```")) {
      if (inCode) {
        out.push("<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>");
        code = [];
        inCode = false;
      } else {
        flushParagraph();
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      code.push(line);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^---+$/.test(line.trim())) {
      flushParagraph();
      closeList();
      out.push("<hr>");
      continue;
    }

    if (line.trim().startsWith(">")) {
      flushParagraph();
      closeList();
      const quote: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      i -= 1;
      out.push("<blockquote><p>" + inline(quote.join(" ")) + "</p></blockquote>");
      continue;
    }

    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      flushParagraph();
      const want = ul ? "ul" : "ol";
      if (listType !== want) {
        closeList();
        out.push("<" + want + ">");
        listType = want;
      }
      out.push("<li>" + inline((ul || ol)![1]) + "</li>");
      continue;
    }

    if (line.trim().startsWith("|") && line.includes("|")) {
      flushParagraph();
      closeList();
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = lines[i]
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((cell) => cell.trim());
        // 分隔行（| --- | --- |）不产出单元格。
        if (!cells.every((cell) => /^:?-{2,}:?$/.test(cell))) rows.push(cells);
        i += 1;
      }
      i -= 1;
      if (rows.length) {
        let table =
          "<table><thead><tr>" +
          rows[0].map((cell) => "<th>" + inline(cell) + "</th>").join("") +
          "</tr></thead><tbody>";
        for (let r = 1; r < rows.length; r += 1) {
          table +=
            "<tr>" +
            rows[r].map((cell) => "<td>" + inline(cell) + "</td>").join("") +
            "</tr>";
        }
        out.push(table + "</tbody></table>");
      }
      continue;
    }

    paragraph.push(line.trim());
  }

  flushParagraph();
  closeList();
  return out.join("\n");
}
