import test from "node:test";
import assert from "node:assert/strict";
import { api, ApiError } from "../src/api/client.ts";

test("attachment transport uses multipart project/file and QA sends IDs only", async () => {
  const original = globalThis.fetch;
  const requests: { path: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path: String(path), init });
    return new Response(JSON.stringify({ attachment_id: "abc", status: "staged" }), { status: 200 });
  };
  try {
    const file = new File(["do not inline this book"], "book.md");
    await api.uploadAssistantAttachment(9, file);
    assert.equal(requests[0]?.path, "/api/assistant/attachments");
    assert.equal(requests[0]?.init?.method, "POST");
    const form = requests[0]?.init?.body as FormData;
    assert.equal(form.get("project_id"), "9");
    assert.equal((form.get("file") as File).name, "book.md");
    assert.equal(requests[0]?.init?.headers, undefined);
    await api.assistantQa({ project_id: 9, query: "请检查附件文本", attachment_ids: ["abc"] });
    assert.deepEqual(JSON.parse(String(requests[1]?.init?.body)), { project_id: 9, query: "请检查附件文本", attachment_ids: ["abc"] });
    await api.getAssistantAttachment(9, "abc");
    assert.equal(requests[2]?.path, "/api/assistant/attachments/abc?project_id=9");
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: "附件过大" }), { status: 413 });
    await assert.rejects(api.uploadAssistantAttachment(9, file), (error: unknown) => error instanceof ApiError && error.status === 413 && error.detail === "附件过大");
  } finally {
    globalThis.fetch = original;
  }
});
