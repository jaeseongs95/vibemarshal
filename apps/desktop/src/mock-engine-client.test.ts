import { describe, expect, it } from "vitest";

import type { ActivatePlanCommand, CommandContext, WorkspaceSnapshot } from "./contracts";
import { MockEngineClient } from "./mock-engine-client";

let counter = 0;

function context(): CommandContext {
  counter += 1;
  return {
    actorRef: "local:test-user",
    clientInstanceId: "vitest:engine-client",
    requestId: `request:${counter}`,
    idempotencyKey: `idempotency:${counter}`,
  };
}

function activation(snapshot: WorkspaceSnapshot, index = 0): ActivatePlanCommand {
  const plan = snapshot.plans[index];
  return {
    planRevisionId: plan.planRevisionId,
    activationDigest: plan.activationDigest,
    expectedGoalDigest: snapshot.goal.definitionDigest,
    expectedState: {
      historySequence: snapshot.historyCursor.sequence,
      goalDigest: snapshot.goal.definitionDigest,
      planDigest: plan.activationDigest,
    },
  };
}

describe("MockEngineClient", () => {
  it("exact Plan activation 뒤 run once 단계만으로 Goal을 완료한다", async () => {
    const engine = new MockEngineClient("golden-path");
    const before = await engine.getSnapshot();
    const activated = await engine.activatePlan(activation(before), context());
    expect(activated.ok).toBe(true);
    if (!activated.ok) return;
    expect(activated.data.tasks[0].status).toBe("ready");

    let current = activated.data;
    for (let index = 0; index < 14; index += 1) {
      const result = await engine.runOnce(context());
      expect(result.ok).toBe(true);
      if (!result.ok) return;
      current = result.data;
    }

    expect(current.phase).toBe("completed");
    expect(current.validation).toMatchObject({ taskValidation: "passed", goalTest: "passed", verdict: "satisfied" });
    expect(current.tasks.every((task) => task.status === "completed")).toBe(true);
    expect(current.history.at(-1)?.eventType).toBe("goal.satisfied");
  });

  it("stale 활성화를 거부하고 최신 revision의 재확인을 요구한다", async () => {
    const engine = new MockEngineClient("stale-activation");
    const before = await engine.getSnapshot();
    const rejected = await engine.activatePlan(activation(before), context());
    expect(rejected.ok).toBe(false);
    if (rejected.ok) return;
    expect(rejected.error.code).toBe("STALE_EXECUTION_INPUT");
    expect(rejected.data.activePlanRevisionId).toBeNull();
    expect(rejected.data.recovery?.kind).toBe("stale_input");

    const refreshed = await engine.refreshStale(context());
    expect(refreshed.ok).toBe(true);
    if (!refreshed.ok) return;
    expect(refreshed.data.plans[0].revisionNo).toBe(5);
    const accepted = await engine.activatePlan(activation(refreshed.data), context());
    expect(accepted.ok).toBe(true);
  });

  it("external_unknown에서는 관측 전 resume과 abandon을 막는다", async () => {
    const engine = new MockEngineClient("external-unknown");
    const earlyResume = await engine.resumeAttempt(context());
    expect(earlyResume.ok).toBe(false);
    if (earlyResume.ok) return;
    expect(earlyResume.error.code).toBe("OBSERVATION_REQUIRED");

    const observed = await engine.observeAttempt(context());
    expect(observed.ok).toBe(true);
    if (!observed.ok) return;
    expect(observed.data.recovery?.stage).toBe("observed");
    expect(observed.data.activeAttempt?.status).toBe("interrupted");

    const resumed = await engine.resumeAttempt(context());
    expect(resumed.ok).toBe(true);
    if (!resumed.ok) return;
    expect(resumed.data.recovery).toBeNull();
    expect(resumed.data.activeAttempt?.threadId).toBe("thread_demo_7f4a");
  });

  it("관측한 intent를 사유와 함께 포기하고 새 Goal 초안에서 복구 상태를 제거한다", async () => {
    const engine = new MockEngineClient("external-unknown");
    await engine.observeAttempt(context());
    const abandoned = await engine.abandonIntent("중복 변경 위험 때문에 새 Plan으로 교체", context());
    expect(abandoned.ok).toBe(true);
    if (!abandoned.ok) return;
    expect(abandoned.data.activePlanRevisionId).toBeNull();
    expect(abandoned.data.plans[0].decision).toBe("needs_revision");
    expect(abandoned.data.history.at(-1)?.summary).toContain("중복 변경 위험");

    const draft = await engine.startGoalRevision(context());
    expect(draft.phase).toBe("goal_draft");
    expect(draft.recovery).toBeNull();
    expect(draft.project.runState).toBe("idle");
    expect(draft.history.at(-1)?.actorRef).toBe("local:test-user");
  });

  it("수정한 Goal 원문과 후보의 결속 digest를 함께 갱신한다", async () => {
    const engine = new MockEngineClient();
    const before = await engine.getSnapshot();
    await engine.startGoalRevision(context());
    const prepared = await engine.prepareGoal("새로운 사용자 요청", context());
    expect(prepared.ok).toBe(true);
    if (!prepared.ok) return;
    expect(prepared.data.goal.definitionDigest).not.toBe(before.goal.definitionDigest);
    expect(prepared.data.goal.definitionDigest).toMatch(/^mock:/);

    const planned = await engine.searchPlans(context());
    expect(planned.ok).toBe(true);
    if (!planned.ok) return;
    expect(planned.data.plans.every((plan) => plan.goalDigest === planned.data.goal.definitionDigest)).toBe(true);
  });
});
