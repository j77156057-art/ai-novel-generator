import test from "node:test";
import assert from "node:assert/strict";
import { createAttachmentDraft, pasteAttachmentFiles } from "../src/utils/assistantAttachments.ts";

const file = (name = "小说.txt") => new File(["正文"], name, { lastModified: 1 });
const metadata = (attachment_id = "attachment-1") => ({ attachment_id, filename: "小说.txt", size: 6, encoding: "utf-8", status: "staged", preview: "正文" });

test("attachments upload once and send only IDs with a default inspection query", async () => {
  let calls = 0;
  const draft = createAttachmentDraft(async (projectId, uploaded) => {
    assert.equal(projectId, 7);
    assert.equal(uploaded.name, "小说.txt");
    calls++;
    return metadata();
  }, String);
  draft.reset(7);
  await draft.add([file(), file()]);
  assert.equal(calls, 1);
  assert.deepEqual(draft.request(""), { project_id: 7, query: "请检查附件文本", attachment_ids: ["attachment-1"] });
  assert.equal(draft.items[0]?.state, "ready");
});

const deferred = () => {
  let resolve!: (value: ReturnType<typeof metadata>) => void;
  const promise = new Promise<ReturnType<typeof metadata>>((done) => { resolve = done; });
  return { promise, resolve };
};

test("pending and failed attachments block sending until removed or retried", async () => {
  const pending = deferred();
  let fail = true;
  const draft = createAttachmentDraft(async () => { await pending.promise; if (fail) throw new Error("文件无法解码"); return metadata(); }, (e) => (e as Error).message);
  draft.reset(7);
  const uploading = draft.add([file()]);
  assert.equal(draft.request("check"), null);
  pending.resolve(metadata());
  await uploading;
  assert.equal(draft.items[0]?.error, "文件无法解码");
  assert.equal(draft.request("check"), null);
  fail = false;
  await draft.retry(draft.items[0]!.key);
  assert.ok(draft.request("check"));
  await draft.add([file("book.pdf")]);
  assert.match(draft.items[1]!.error, /仅支持/);
  assert.equal(draft.request("check"), null);
  draft.remove(draft.items[1]!.key);
  assert.ok(draft.request("check"));
});

test("removed upload cannot revive a chip or overwrite its replacement", async () => {
  const pending = deferred();
  let calls = 0;
  const draft = createAttachmentDraft(async () => ++calls === 1 ? pending.promise : metadata("new"), String);
  draft.reset(7);
  const uploading = draft.add([file()]);
  draft.remove(draft.items[0]!.key);
  await draft.add([file()]);
  pending.resolve(metadata("old"));
  await uploading;
  assert.deepEqual(draft.request("check")?.attachment_ids, ["new"]);
});

test("project changes including A to B to A invalidate pending uploads", async () => {
  const pending = deferred();
  const draft = createAttachmentDraft(async () => pending.promise, String);
  draft.reset(7);
  const uploading = draft.add([file()]);
  draft.reset(8);
  draft.reset(7);
  pending.resolve(metadata());
  await uploading;
  assert.equal(draft.items.length, 0);
  assert.deepEqual(draft.request("text"), { project_id: 7, query: "text" });
  assert.equal(draft.request(""), null);
  draft.reset(NaN);
  assert.equal(draft.request("text"), null);
});

test("file paste preserves accompanying text and replaces only the selection", () => {
  let prevented = false;
  const event = { preventDefault: () => { prevented = true; }, clipboardData: { files: [file()], getData: () => "备注" } };
  const result = pasteAttachmentFiles(event, "前旧后", 1, 2);
  assert.equal(prevented, true);
  assert.equal(result?.text, "前备注后");
  assert.equal(result?.caret, 3);
  assert.equal(result?.files.length, 1);
  prevented = false;
  assert.equal(pasteAttachmentFiles({ ...event, clipboardData: { files: [], getData: () => "text" } }, "前", 1, 1), null);
  assert.equal(prevented, false);
});

test("Vue reactive draft accepts upload completion and retry", async () => {
  const { reactive, computed } = await import("vue");
  const pending = deferred();
  const draft = reactive(createAttachmentDraft(async () => pending.promise, String));
  draft.reset(7);
  const canSend = computed(() => draft.request("") !== null);
  const uploading = draft.add([file()]);
  assert.equal(canSend.value, false);
  pending.resolve(metadata());
  await uploading;
  assert.equal(canSend.value, true);
  assert.equal(draft.items[0]?.state, "ready");
  draft.reset(8);
  assert.equal(canSend.value, false);
});

test("file-only paste leaves selected existing text intact", () => {
  const event = { preventDefault() {}, clipboardData: { files: [file()], getData: () => "" } };
  assert.equal(pasteAttachmentFiles(event, "原有文本", 0, 4)?.text, "原有文本");
});
