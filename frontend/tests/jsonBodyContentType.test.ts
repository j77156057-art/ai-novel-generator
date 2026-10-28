import test from "node:test";
import assert from "node:assert/strict";
import { api } from "../src/api/client.ts";

/**
 * 带 body 的 POST 必须带 `Content-Type: application/json`。
 *
 * ## 为什么要有这条测试
 *
 * fetch 对**字符串 body** 不会自动带 `application/json` —— 浏览器会补
 * `Content-Type: text/plain;charset=UTF-8`。FastAPI 收到 `text/plain` 的请求体
 * **不**当 JSON 解析 ⇒ 直接 422
 * （实测：`{"distill_id": 3}` 被拒成「Input should be a valid dictionary」）。
 *
 * 之前 `jsonInit("POST")` 不带 body 时不设 headers，调用方自己补
 * `body: JSON.stringify(...)` ⇒ Content-Type 永远是 text/plain
 * ⇒ **「应用到本项目」那个按钮一直是坏的**（真机点出来才发现，测试没抓到）。
 */
function captureFetch() {
  const requests: { path: string; init?: RequestInit }[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    requests.push({ path: String(path), init });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  return { requests, restore: () => { globalThis.fetch = original; } };
}

function contentType(init?: RequestInit): string | undefined {
  const headers = init?.headers as Record<string, string> | undefined;
  if (!headers) return undefined;
  return headers["Content-Type"] ?? headers["content-type"];
}

test("应用到本项目：带 application/json 的请求体", async () => {
  const cap = captureFetch();
  try {
    await api.applyStyleDistill(1, 3);
    assert.equal(cap.requests[0]?.init?.method, "POST");
    assert.equal(
      contentType(cap.requests[0]?.init),
      "application/json",
      "Content-Type 必须是 application/json —— text/plain 会被 FastAPI 拒成 422",
    );
    assert.deepEqual(JSON.parse(String(cap.requests[0]?.init?.body)), { distill_id: 3 });
  } finally {
    cap.restore();
  }
});

test("修档案：带 application/json 的请求体", async () => {
  const cap = captureFetch();
  try {
    await api.repairStyleNarrative(1, { word: "下一刻", mode: "remove_word" });
    assert.equal(cap.requests[0]?.init?.method, "POST");
    assert.equal(contentType(cap.requests[0]?.init), "application/json");
    assert.deepEqual(JSON.parse(String(cap.requests[0]?.init?.body)), {
      word: "下一刻",
      mode: "remove_word",
    });
  } finally {
    cap.restore();
  }
});

test("不带 body 的 POST 不该硬塞 Content-Type", async () => {
  const cap = captureFetch();
  try {
    await api.proposePremise(1);
    assert.equal(cap.requests[0]?.init?.method, "POST");
    assert.equal(cap.requests[0]?.init?.body, undefined);
    assert.equal(contentType(cap.requests[0]?.init), undefined);
  } finally {
    cap.restore();
  }
});
