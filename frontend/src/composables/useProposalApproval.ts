/**
 * 「批准并写入」提案的本地状态与动作。
 *
 * 从 `AssistantWorkspace.vue` 抽出：这是卡片上唯一一条**真正写库**的路径，
 * 与渲染分开后，卡片组件只负责 emit，写入的顺序（批准 → 应用）、忙态、
 * 失败回执都集中在这里，避免和别的卡片逻辑搅在一起。
 */
import { reactive } from "vue";
import { ApiError, api } from "@/api/client";
import type { ProjectOut } from "@/api/types";

/** 批准逻辑只需要卡片上的这几个字段。 */
export interface ApprovalCard {
  key: string;
  notice: string;
  applied?: boolean;
  result: Record<string, unknown> | null;
}

export function useProposalApproval<C extends ApprovalCard>(opts: {
  projectId: number;
  /** 写入成功后把最新项目写回页面（设定/角色卡随之刷新）。 */
  setProject: (project: ProjectOut) => void;
  /** 写入成功后的留痕与刷新（由页面负责）。 */
  onApplied: (card: C, versionId: number) => void;
  /** 写入失败后的留痕（由页面负责）。 */
  onFailed: (card: C, message: string) => void;
  statusOf: (err: unknown) => number | null;
  textOf: (err: unknown) => string;
}) {
  /** 忙态按卡片 key 维护：同一张卡不允许并发两次写入。 */
  const promoteBusy = reactive<Record<string, boolean>>({});

  /** 卡片结果里是否带着一条「已创建、待批准」的提案。 */
  function proposalIdOf(card: C): number | null {
    const proposal = card.result?.proposal;
    if (!proposal || typeof proposal !== "object") return null;
    const id = (proposal as Record<string, unknown>).id;
    return typeof id === "number" ? id : null;
  }

  /**
   * 提案卡确认后只创建了一条 pending 提案，业务表还没动。
   *
   * 这里把「批准 → 写入」两步补在同一张卡上：用户不用再跳去项目页找。
   * 仍然保持两次状态转换（不合成一键写库），只是路径更短。
   */
  async function approveAndApplyCard(card: C): Promise<void> {
    const id = proposalIdOf(card);
    if (id === null || promoteBusy[card.key]) return;
    promoteBusy[card.key] = true;
    card.notice = "正在批准并写入…";
    try {
      try {
        await api.approveProposal(id);
      } catch (err) {
        // 已经批准过、状态机不允许重复批准：继续尝试写入；其他错误照常抛。
        if (!(err instanceof ApiError && err.status === 409)) throw err;
      }
      const res = await api.applyProposal(id);
      card.applied = true;
      card.notice = `已写入项目（Version #${res.version_id}）。`;
      opts.onApplied(card, res.version_id);
      // 设定可能刚变：刷新项目，右侧上下文/角色卡跟着更新。
      api
        .getProject(opts.projectId)
        .then((p) => opts.setProject(p))
        .catch(() => {
          /* 刷新失败不影响写入结论 */
        });
    } catch (err) {
      const message = opts.textOf(err);
      card.notice = `写入失败（HTTP ${opts.statusOf(err) ?? "?"}）：${message}`;
      opts.onFailed(card, message);
    } finally {
      promoteBusy[card.key] = false;
    }
  }

  return { promoteBusy, proposalIdOf, approveAndApplyCard };
}
