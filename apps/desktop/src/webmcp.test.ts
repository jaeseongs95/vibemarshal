import { describe, expect, it, vi } from "vitest";

import type { ActivatePlanCommand, CommandContext, WorkspaceSnapshot } from "./contracts";
import { MockEngineClient } from "./mock-engine-client";
import { registerRunOnceWebMcpTool } from "./webmcp";

function context(): CommandContext {
  return { actorRef: "local:webmcp-test", clientInstanceId: "vitest:webmcp", requestId: "request:webmcp", idempotencyKey: "idempotency:webmcp" };
}

function activation(snapshot: WorkspaceSnapshot): ActivatePlanCommand {
  const plan = snapshot.plans[0];
  return {
    planRevisionId: plan.planRevisionId,
    activationDigest: plan.activationDigest,
    expectedGoalDigest: snapshot.goal.definitionDigest,
    expectedState: { historySequence: snapshot.historyCursor.sequence, goalDigest: snapshot.goal.definitionDigest, planDigest: plan.activationDigest },
  };
}

describe("run_vibemarshal_next_step WebMCP", () => {
  it("같은 EngineClient 명령을 등록·실행하고 AbortSignal로 해제한다", async () => {
    const engine = new MockEngineClient();
    const snapshot = await engine.getSnapshot();
    await engine.activatePlan(activation(snapshot), context());
    let registeredTool: { name: string; annotations?: { readOnlyHint?: boolean }; execute(input: unknown): unknown | Promise<unknown> } | undefined;
    let registeredSignal: AbortSignal | undefined;
    const documentRef = {
      modelContext: {
        registerTool(tool: typeof registeredTool, options?: { signal?: AbortSignal }) {
          registeredTool = tool;
          registeredSignal = options?.signal;
        },
      },
    } as unknown as Document & { modelContext: { registerTool(tool: NonNullable<typeof registeredTool>, options?: { signal?: AbortSignal }): void } };
    const onResult = vi.fn();

    const unregister = registerRunOnceWebMcpTool({ engine, createContext: context, onResult, documentRef });
    expect(registeredTool?.name).toBe("run_vibemarshal_next_step");
    expect(registeredTool?.annotations?.readOnlyHint).toBe(false);
    const output = await registeredTool!.execute({});
    expect(output).toMatchObject({ ok: true, phase: "running", activeTask: "T1" });
    expect(onResult).toHaveBeenCalledOnce();

    await expect(registeredTool!.execute({ unexpected: true })).rejects.toThrow("빈 객체");
    unregister();
    expect(registeredSignal?.aborted).toBe(true);
  });
});
