export function pasteAttachmentFiles(
  event: { preventDefault(): void; clipboardData: { files: ArrayLike<File>; getData(type: string): string } | null },
  text: string,
  start: number,
  end: number,
) {
  const files = Array.from(event.clipboardData?.files ?? []);
  if (!files.length) return null;
  event.preventDefault();
  const pasted = event.clipboardData?.getData("text/plain") ?? "";
  return { files, text: pasted ? text.slice(0, start) + pasted + text.slice(end) : text, caret: start + pasted.length };
}

export interface AttachmentMetadata {
  attachment_id: string;
  filename: string;
  size: number;
  encoding: string;
  status: string;
  preview: string;
}

export interface DraftAttachment {
  key: number;
  file: File;
  state: "uploading" | "ready" | "failed";
  metadata?: AttachmentMetadata;
  error: string;
}

/** The owning Vue component makes this object reactive; methods use their receiver. */
export function createAttachmentDraft(
  upload: (projectId: number, file: File) => Promise<AttachmentMetadata>,
  errorText: (error: unknown) => string,
) {
  let sequence = 0;
  return {
    projectId: 0,
    epoch: 0,
    items: [] as DraftAttachment[],
    reset(projectId: number) {
      this.projectId = projectId;
      this.epoch++;
      this.items = [];
    },
    async add(files: File[]) {
      const added: DraftAttachment[] = [];
      for (const file of files) {
        if (this.items.some((item) => item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified)) continue;
        const item: DraftAttachment = { key: ++sequence, file, state: "uploading", error: "" };
        this.items.push(item);
        added.push(this.items[this.items.length - 1]!);
      }
      await Promise.all(added.map((item) => this.runUpload(item)));
    },
    async runUpload(item: DraftAttachment) {
      const epoch = this.epoch;
      const projectId = this.projectId;
      try {
        if (!/\.(txt|md)$/i.test(item.file.name)) throw new Error("仅支持 .txt 或 .md 文本文件，请转换格式后重新添加。");
        const metadata = await upload(projectId, item.file);
        if (epoch !== this.epoch || !this.items.includes(item)) return;
        if (!metadata.attachment_id || metadata.status === "failed" || metadata.status === "error") throw new Error("附件未能读取，请重新上传。");
        item.metadata = metadata;
        item.state = "ready";
      } catch (error) {
        if (epoch !== this.epoch || !this.items.includes(item)) return;
        item.state = "failed";
        item.error = errorText(error);
      }
    },
    remove(key: number) {
      this.items = this.items.filter((item) => item.key !== key);
    },
    async retry(key: number) {
      const previous = this.items.find((item) => item.key === key);
      if (!previous || previous.state !== "failed") return;
      this.remove(key);
      await this.add([previous.file]);
    },
    request(text: string) {
      if (!Number.isInteger(this.projectId) || this.projectId <= 0 || this.items.some((item) => item.state !== "ready")) return null;
      const query = text.trim() || (this.items.length ? "请检查附件文本" : "");
      if (!query) return null;
      return {
        project_id: this.projectId,
        query,
        ...(this.items.length ? { attachment_ids: this.items.map((item) => item.metadata!.attachment_id) } : {}),
      };
    },
  };
}
