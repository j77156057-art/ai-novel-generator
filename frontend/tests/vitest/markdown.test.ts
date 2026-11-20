import { describe, expect, it } from "vitest";
import { renderMarkdown } from "@/utils/markdown";

describe("renderMarkdown", () => {
  it("渲染标题 / 粗体 / 列表 / 引用", () => {
    const html = renderMarkdown(
      "### 方向一\n\n**先收紧**\n\n1. 第一步\n2. 第二步\n\n> 好处：更有劲",
    );
    expect(html).toContain("<h3>方向一</h3>");
    expect(html).toContain("<strong>先收紧</strong>");
    expect(html).toContain("<ol>");
    expect(html).toContain("<li>第一步</li>");
    expect(html).toContain("<blockquote>");
  });

  it("转义模型输出里的 HTML，不把标签当结构", () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)"> 与 **重点**');
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
    expect(html).toContain("<strong>重点</strong>");
  });

  it("渲染行内代码与表格", () => {
    const html = renderMarkdown(
      "用 `plot_bible` 记录\n\n| 方向 | 风险 |\n| --- | --- |\n| 设局 | 反派降智 |",
    );
    expect(html).toContain("<code>plot_bible</code>");
    expect(html).toContain("<table>");
    expect(html).toContain("<td>设局</td>");
  });

  it("空文本返回空串", () => {
    expect(renderMarkdown("")).toBe("");
  });
});
