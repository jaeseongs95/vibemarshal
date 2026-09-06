import type {
  ActivatePlanCommand,
  CommandContext,
  CommandResult,
  EngineClient,
  HistoryEventView,
  PlanCandidateView,
  PossibleAction,
  PossibleActionKind,
  RunLogEntry,
  RunOnceAction,
  ScenarioKey,
  TaskRuntimeStatus,
  TaskView,
  WorkspacePhase,
  WorkspaceSnapshot,
} from "./contracts";

const DIGESTS = {
  goal: `sha256:${"a4".repeat(32)}`,
  planA: `sha256:${"74".repeat(32)}`,
  planANew: `sha256:${"86".repeat(32)}`,
  planB: `sha256:${"3c".repeat(32)}`,
  definitionA: `sha256:${"19".repeat(32)}`,
  definitionB: `sha256:${"2d".repeat(32)}`,
  empty: `sha256:${"00".repeat(32)}`,
} as const;

const now = () => new Date().toISOString();

function demoDigest(value: string): string {
  let hash = 0x811c9dc5;
  for (const character of value) {
    hash ^= character.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193);
  }
  return `mock:${(hash >>> 0).toString(16).padStart(8, "0").repeat(8)}`;
}

function eventHash(sequence: number): string {
  return `sha256:${sequence.toString(16).padStart(64, "0")}`;
}

function action(
  kind: PossibleActionKind,
  confirmation: PossibleAction["confirmation"] = "none",
  checkpointRequired = false,
): PossibleAction {
  return { kind, enabled: true, disabledReason: null, confirmation, checkpointRequired };
}

function baseGoal(): WorkspaceSnapshot["goal"] {
  return {
    goalId: "goal_demo_reliability",
    revisionId: "goal_revision_demo_03",
    revisionNo: 3,
    status: "active",
    sourceRequest:
      "사용량이 비어 있는 실행에서도 최종 보고가 중단되지 않도록 합산 경계를 수정하고 기존 동작을 검증해줘.",
    sourceRequestDigest: `sha256:${"91".repeat(32)}`,
    definitionDigest: DIGESTS.goal,
    observableOutcome:
      "usage 일부가 null이어도 Markdown과 JSON 보고가 생성되고 기존 정상 합계가 유지된다.",
    missionClass: "bugfix_stabilization",
    criteria: [
      {
        criterionId: "ac_report_null",
        statement: "null usage 값이 있어도 최종 보고가 오류 없이 생성된다.",
        validationIntent: "Markdown·JSON 두 경로를 별도로 실행해 정상 종료와 결측 표시를 확인한다.",
      },
      {
        criterionId: "ac_preserve_totals",
        statement: "측정 가능한 기존 token·latency 합계는 바뀌지 않는다.",
        validationIntent: "기존 정상 fixture와 변경 후 결과를 비교한다.",
      },
      {
        criterionId: "ac_unknown_not_zero",
        statement: "미제공 usage는 0이 아니라 알 수 없음과 사유로 표시된다.",
        validationIntent: "결측 fixture의 직렬화 결과를 검사한다.",
      },
    ],
    constraints: ["기존 CLI JSON key와 exit code를 보존한다.", "legacy/prototype source는 수정하지 않는다."],
    nonGoals: ["실제 provider usage를 추정해 채우지 않는다.", "Engine 1.0 cutover 판정을 변경하지 않는다."],
    effectPolicy: {
      mutation: "minimal_change",
      behavior: "preserve_public_contracts",
      prohibitedEffects: ["배포", "외부 메시지", "운영 DB 변경"],
    },
    preparationBinding: {
      normalizationStatus: "complete",
      independentReview: "passed",
      reviewerRole: "goal_reviewer",
    },
  };
}

function planCandidates(staleResolved = false): PlanCandidateView[] {
  const firstId = staleResolved ? "plan_revision_demo_05" : "plan_revision_demo_04";
  return [
    {
      planRevisionId: firstId,
      revisionNo: staleResolved ? 5 : 4,
      name: "경계 수정 + 양방향 회귀",
      summary: "합산기를 한곳에서 결측 안전하게 만들고 Markdown·JSON 출력 계약을 함께 검증합니다.",
      decision: "admissible",
      score: 92,
      activationDigest: staleResolved ? DIGESTS.planANew : DIGESTS.planA,
      definitionDigest: DIGESTS.definitionA,
      goalDigest: DIGESTS.goal,
      risk: "low",
      checkpoint: "외부 효과 없음",
      modelEnvelope: "executor · validator 분리",
      validationSummary: "Task validation 3개 · 독립 Goal Test 1개",
      tasks: [
        { taskRef: "T1", title: "현재 실패 재현", contributesTo: ["AC-01"], dependencies: [] },
        { taskRef: "T2", title: "usage 합산 경계 수정", contributesTo: ["AC-01", "AC-03"], dependencies: ["T1"] },
        { taskRef: "T3", title: "보고 회귀 검증", contributesTo: ["AC-01", "AC-02", "AC-03"], dependencies: ["T2"] },
      ],
    },
    {
      planRevisionId: "plan_revision_demo_alt_02",
      revisionNo: 2,
      name: "출력기별 방어 처리",
      summary: "Markdown과 JSON 출력기에서 각각 null 값을 처리해 변경 범위를 좁힙니다.",
      decision: "admissible",
      score: 78,
      activationDigest: DIGESTS.planB,
      definitionDigest: DIGESTS.definitionB,
      goalDigest: DIGESTS.goal,
      risk: "medium",
      checkpoint: "외부 효과 없음",
      modelEnvelope: "executor · validator 분리",
      validationSummary: "Task validation 4개 · 독립 Goal Test 1개",
      tasks: [
        { taskRef: "T1", title: "현재 실패 재현", contributesTo: ["AC-01"], dependencies: [] },
        { taskRef: "T2", title: "Markdown 출력 방어", contributesTo: ["AC-01"], dependencies: ["T1"] },
        { taskRef: "T3", title: "JSON 출력 방어", contributesTo: ["AC-01"], dependencies: ["T1"] },
        { taskRef: "T4", title: "두 출력 경로 비교", contributesTo: ["AC-02", "AC-03"], dependencies: ["T2", "T3"] },
      ],
    },
  ];
}

function runtimeTasks(): TaskView[] {
  return [
    {
      taskId: "task_demo_01",
      taskRef: "T1",
      title: "현재 실패 재현",
      description: "null token과 latency 조합에서 두 보고 경로의 실패를 고정합니다.",
      status: "ready",
      dependencies: [],
      contributesTo: ["AC-01"],
      executionSpecRevisionId: null,
      attemptId: null,
      updatedAt: now(),
    },
    {
      taskId: "task_demo_02",
      taskRef: "T2",
      title: "usage 합산 경계 수정",
      description: "결측과 0을 구분하는 공통 합산 결과를 도입합니다.",
      status: "pending",
      dependencies: ["T1"],
      contributesTo: ["AC-01", "AC-03"],
      executionSpecRevisionId: null,
      attemptId: null,
      updatedAt: now(),
    },
    {
      taskId: "task_demo_03",
      taskRef: "T3",
      title: "보고 회귀 검증",
      description: "기존 정상 합계와 결측 표시를 별도 실행으로 비교합니다.",
      status: "pending",
      dependencies: ["T2"],
      contributesTo: ["AC-01", "AC-02", "AC-03"],
      executionSpecRevisionId: null,
      attemptId: null,
      updatedAt: now(),
    },
  ];
}

function initialHistory(): HistoryEventView[] {
  return [
    {
      sequence: 39,
      eventHash: eventHash(39),
      eventType: "goal.reviewed",
      entityType: "goal_revision",
      entityId: "goal_revision_demo_03",
      summary: "독립 검토에서 blocking finding이 없었습니다.",
      actorRef: "local:user",
      source: "desktop-gui",
      occurredAt: "2026-09-06T08:40:00.000Z",
    },
    {
      sequence: 40,
      eventHash: eventHash(40),
      eventType: "plan.candidate_admitted",
      entityType: "plan_revision",
      entityId: "plan_revision_demo_04",
      summary: "후보 A가 결정적 Gate를 통과했습니다.",
      actorRef: "engine:core",
      source: "planning-pipeline",
      occurredAt: "2026-09-06T08:41:12.000Z",
    },
    {
      sequence: 41,
      eventHash: eventHash(41),
      eventType: "plan.search_completed",
      entityType: "project",
      entityId: "project_flowmarshal_demo",
      summary: "비교 가능한 Plan 후보 2개가 준비됐습니다.",
      actorRef: "local:user",
      source: "desktop-gui",
      occurredAt: "2026-09-06T08:42:03.000Z",
    },
  ];
}

function possibleActionsFor(snapshot: WorkspaceSnapshot): PossibleAction[] {
  if (snapshot.recovery?.kind === "stale_input") return [action("refresh_snapshot")];
  if (snapshot.recovery?.kind === "external_unknown") {
    return snapshot.recovery.stage === "detected"
      ? [action("observe_attempt")]
      : [action("resume_attempt"), action("abandon_intent", "external_effect", true)];
  }
  switch (snapshot.phase) {
    case "goal_draft":
      return [action("prepare_goal")];
    case "goal_ready":
      return [action("search_plans"), action("edit_goal")];
    case "plan_ready":
      return [action("activate_plan", "plan_activation"), action("edit_goal")];
    case "plan_active":
    case "running":
      return [action("run_once")];
    case "completed":
      return [action("edit_goal")];
  }
  return [];
}

function initialSnapshot(scenario: ScenarioKey): WorkspaceSnapshot {
  const goal = baseGoal();
  const history = initialHistory();
  const snapshot: WorkspaceSnapshot = {
    apiVersion: "1",
    engineSchemaVersion: 2,
    productStatus: "NO-GO",
    generatedAt: now(),
    scenario,
    phase: "plan_ready",
    project: {
      projectId: "project_flowmarshal_demo",
      name: "FlowMarshal Engine",
      location: {
        locationRef: "project-location:flowmarshal-demo",
        displayName: "flowmarshal",
        displayPath: "D:\\codex\\flowmarshal",
      },
      runState: "idle",
      activeBlocker: null,
    },
    goal,
    plans: planCandidates(false),
    recommendedPlanRevisionId: "plan_revision_demo_04",
    activePlanRevisionId: null,
    tasks: [],
    validation: {
      workerObservation: "pending",
      taskValidation: "pending",
      goalTest: "pending",
      verdict: "unknown",
    },
    activeAttempt: null,
    recovery: null,
    history,
    historyCursor: { sequence: 41, eventHash: eventHash(41) },
    budget: {
      inputTokens: 18420,
      cachedInputTokens: 11280,
      outputTokens: 2430,
      latencyMs: 48200,
      availabilityReason: null,
    },
    possibleActions: [],
    runLog: [],
  };

  if (scenario === "external-unknown") {
    snapshot.phase = "recovery";
    snapshot.activePlanRevisionId = snapshot.plans[0].planRevisionId;
    snapshot.plans[0].decision = "selected";
    snapshot.tasks = runtimeTasks().map((task, index) => ({
      ...task,
      status: index === 0 ? "completed" : index === 1 ? "running" : "pending",
      attemptId: index === 1 ? "attempt_demo_02" : null,
      executionSpecRevisionId: index === 1 ? "execution_spec_demo_02" : null,
    }));
    snapshot.project.runState = "recovery_required";
    snapshot.project.activeBlocker = "EXTERNAL_UNKNOWN";
    snapshot.activeAttempt = {
      attemptId: "attempt_demo_02",
      taskId: "task_demo_02",
      status: "unknown",
      threadId: "thread_demo_7f4a",
      turnId: "turn_demo_aa32",
      intentId: "intent_demo_resume_02",
      receiptId: null,
    };
    snapshot.recovery = {
      kind: "external_unknown",
      stage: "detected",
      title: "시작 intent 뒤 receipt를 확인하지 못했습니다",
      explanation: "새 turn을 만들면 같은 수정이 중복 적용될 수 있습니다. 저장된 thread를 재개하지 않고 먼저 읽습니다.",
      expectedDigest: DIGESTS.planA,
      currentDigest: DIGESTS.planA,
      intentId: "intent_demo_resume_02",
      receiptState: "missing",
      lastCheckpoint: "T1 Task validation 통과 · State 재관측 완료",
      evidence: ["thread binding 존재", "turn start intent 존재", "provider receipt 없음"],
    };
  }

  snapshot.possibleActions = possibleActionsFor(snapshot);
  return snapshot;
}

export class MockEngineClient implements EngineClient {
  private snapshot: WorkspaceSnapshot;
  private readonly listeners = new Set<(snapshot: WorkspaceSnapshot) => void>();
  private staleTriggered = false;

  constructor(scenario: ScenarioKey = "golden-path") {
    this.snapshot = initialSnapshot(scenario);
  }

  async getSnapshot(): Promise<WorkspaceSnapshot> {
    return structuredClone(this.snapshot);
  }

  subscribe(listener: (snapshot: WorkspaceSnapshot) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async resetScenario(scenario: ScenarioKey): Promise<WorkspaceSnapshot> {
    this.staleTriggered = false;
    this.snapshot = initialSnapshot(scenario);
    this.emit();
    return this.getSnapshot();
  }

  async startGoalRevision(context: CommandContext): Promise<WorkspaceSnapshot> {
    const goal = baseGoal();
    goal.status = "draft";
    goal.revisionNo += 1;
    goal.revisionId = "goal_revision_demo_04";
    goal.preparationBinding = {
      normalizationStatus: "pending",
      independentReview: "pending",
      reviewerRole: "goal_reviewer",
    };
    const baseline = initialSnapshot(this.snapshot.scenario);
    this.snapshot = {
      ...baseline,
      phase: "goal_draft",
      project: { ...baseline.project, runState: "idle", activeBlocker: null },
      goal,
      plans: [],
      recommendedPlanRevisionId: null,
      activePlanRevisionId: null,
      tasks: [],
      activeAttempt: null,
      recovery: null,
      history: this.snapshot.history,
      historyCursor: this.snapshot.historyCursor,
    };
    this.commit("goal.revision_started", "새 Goal revision 초안을 시작했습니다.", "goal_revision", goal.revisionId, context);
    return this.getSnapshot();
  }

  async prepareGoal(sourceRequest: string, context: CommandContext): Promise<CommandResult> {
    if (!sourceRequest.trim()) return this.error("GOAL_INPUT_REQUIRED", "사용자 원문이 필요합니다.");
    this.snapshot.goal.sourceRequest = sourceRequest.trim();
    this.snapshot.goal.sourceRequestDigest = demoDigest(this.snapshot.goal.sourceRequest);
    this.snapshot.goal.definitionDigest = demoDigest(JSON.stringify({
      sourceRequest: this.snapshot.goal.sourceRequest,
      criteria: this.snapshot.goal.criteria,
      constraints: this.snapshot.goal.constraints,
      nonGoals: this.snapshot.goal.nonGoals,
      effectPolicy: this.snapshot.goal.effectPolicy,
    }));
    this.snapshot.goal.status = "active";
    this.snapshot.goal.preparationBinding = {
      normalizationStatus: "complete",
      independentReview: "passed",
      reviewerRole: "goal_reviewer",
    };
    this.snapshot.phase = "goal_ready";
    this.commit("goal.prepared", "Goal 정규화와 독립 검토를 완료했습니다.", "goal_revision", this.snapshot.goal.revisionId, context);
    return { ok: true, data: await this.getSnapshot() };
  }

  async searchPlans(context: CommandContext): Promise<CommandResult> {
    this.snapshot.plans = planCandidates(false).map((plan) => ({ ...plan, goalDigest: this.snapshot.goal.definitionDigest }));
    this.snapshot.recommendedPlanRevisionId = this.snapshot.plans[0].planRevisionId;
    this.snapshot.phase = "plan_ready";
    this.commit("plan.search_completed", "비교 가능한 Plan 후보 2개가 준비됐습니다.", "project", this.snapshot.project.projectId, context);
    return { ok: true, data: await this.getSnapshot() };
  }

  async activatePlan(command: ActivatePlanCommand, context: CommandContext): Promise<CommandResult> {
    const plan = this.snapshot.plans.find((candidate) => candidate.planRevisionId === command.planRevisionId);
    if (!plan || plan.activationDigest !== command.activationDigest) {
      return this.error("DIGEST_MISMATCH", "현재 후보와 activation digest가 일치하지 않습니다.");
    }
    if (command.expectedGoalDigest !== this.snapshot.goal.definitionDigest) {
      return this.error("REVISION_CONFLICT", "Goal digest가 현재 revision과 다릅니다.");
    }
    if (plan.goalDigest !== this.snapshot.goal.definitionDigest) {
      return this.error("REVISION_CONFLICT", "Plan 후보가 현재 Goal revision에 결속되지 않았습니다.");
    }

    if (this.snapshot.scenario === "stale-activation" && !this.staleTriggered) {
      this.staleTriggered = true;
      const expected = plan.activationDigest;
      this.snapshot.plans = planCandidates(true).map((candidate) => ({ ...candidate, goalDigest: this.snapshot.goal.definitionDigest }));
      this.snapshot.recommendedPlanRevisionId = this.snapshot.plans[0].planRevisionId;
      this.snapshot.phase = "recovery";
      this.snapshot.project.runState = "recovery_required";
      this.snapshot.project.activeBlocker = "STALE_EXECUTION_INPUT";
      this.snapshot.recovery = {
        kind: "stale_input",
        stage: "detected",
        title: "확인 중 Project Map이 바뀌었습니다",
        explanation: "보던 후보를 묵시적으로 승인하지 않습니다. 최신 revision의 의미 diff를 다시 확인해야 합니다.",
        expectedDigest: expected,
        currentDigest: DIGESTS.planANew,
        intentId: null,
        receiptState: "not_applicable",
        lastCheckpoint: "Plan 활성화 전 · provider 호출 0회",
        evidence: ["History sequence 41 → 42", "Project Map semantic digest 변경", "새 Plan revision 5 생성"],
      };
      this.commit("plan.activation_rejected", "stale 입력으로 활성화를 거부했습니다.", "plan_revision", plan.planRevisionId, context);
      return this.error("STALE_EXECUTION_INPUT", "Plan 입력이 최신 상태와 다릅니다.", "refresh_snapshot");
    }

    if (command.expectedState.historySequence !== this.snapshot.historyCursor.sequence) {
      return this.error("REVISION_CONFLICT", "History cursor가 최신 상태와 다릅니다.", "refresh_snapshot");
    }

    this.snapshot.activePlanRevisionId = plan.planRevisionId;
    this.snapshot.plans = this.snapshot.plans.map((candidate) => ({
      ...candidate,
      decision: candidate.planRevisionId === plan.planRevisionId ? "selected" : "admissible",
    }));
    this.snapshot.tasks = runtimeTasks();
    this.snapshot.phase = "plan_active";
    this.snapshot.project.runState = "active";
    this.snapshot.project.activeBlocker = null;
    this.commit("plan.activated", "정확한 revision과 digest로 Plan을 활성화했습니다.", "plan_revision", plan.planRevisionId, context);
    return { ok: true, data: await this.getSnapshot() };
  }

  async runOnce(context: CommandContext): Promise<CommandResult> {
    if (!this.snapshot.activePlanRevisionId || this.snapshot.recovery) {
      return this.error("RUN_BLOCKED", "활성 Plan 또는 복구 확인이 필요합니다.");
    }

    const taskIndex = this.snapshot.tasks.findIndex((task) => task.status !== "completed");
    if (taskIndex < 0) return this.advanceGoalTest(context);

    const task = this.snapshot.tasks[taskIndex];
    let actionValue: RunOnceAction;
    let detail: string;
    let lane: RunLogEntry["lane"] = "worker";
    let nextStatus: TaskRuntimeStatus;

    switch (task.status) {
      case "ready":
        actionValue = "materialized";
        nextStatus = "materialized";
        task.executionSpecRevisionId = `execution_spec_${task.taskRef.toLowerCase()}_01`;
        detail = `${task.taskRef} Execution Spec과 Context Pack을 고정했습니다.`;
        break;
      case "materialized":
      case "reserved":
        actionValue = "dispatched";
        nextStatus = "running";
        task.attemptId = `attempt_${task.taskRef.toLowerCase()}_01`;
        this.snapshot.activeAttempt = {
          attemptId: task.attemptId,
          taskId: task.taskId,
          status: "running",
          threadId: `thread_${task.taskRef.toLowerCase()}_demo`,
          turnId: `turn_${task.taskRef.toLowerCase()}_demo`,
          intentId: `intent_${task.taskRef.toLowerCase()}_start`,
          receiptId: `receipt_${task.taskRef.toLowerCase()}_start`,
        };
        detail = `${task.taskRef} Worker turn을 시작하고 receipt를 결속했습니다.`;
        break;
      case "running":
        actionValue = "observed";
        nextStatus = "validating";
        lane = "task_validation";
        this.snapshot.validation.workerObservation = "collected";
        this.snapshot.validation.taskValidation = "running";
        if (this.snapshot.activeAttempt) this.snapshot.activeAttempt.status = "succeeded";
        detail = `${task.taskRef} Worker 결과를 관측하고 Task validation을 시작했습니다.`;
        break;
      case "validating":
        actionValue = "completed";
        nextStatus = "completed";
        lane = "task_validation";
        this.snapshot.validation.taskValidation = "passed";
        this.snapshot.activeAttempt = null;
        detail = `${task.taskRef} 필수 evidence와 validation이 통과했습니다.`;
        break;
      default:
        return this.error("RUN_BLOCKED", `${task.taskRef} 상태에서는 실행할 수 없습니다.`);
    }

    task.status = nextStatus;
    task.updatedAt = now();
    if (nextStatus === "completed" && this.snapshot.tasks[taskIndex + 1]) {
      this.snapshot.tasks[taskIndex + 1].status = "ready";
      this.snapshot.tasks[taskIndex + 1].updatedAt = now();
      this.snapshot.validation.workerObservation = "pending";
      this.snapshot.validation.taskValidation = "pending";
    }
    this.snapshot.phase = "running";
    this.commit(`run_once.${actionValue}`, detail, "task", task.taskId, context, { action: actionValue, lane, taskRef: task.taskRef, detail });
    return { ok: true, data: await this.getSnapshot() };
  }

  async refreshStale(context: CommandContext): Promise<CommandResult> {
    if (this.snapshot.recovery?.kind !== "stale_input") return this.error("NO_STALE_INPUT", "새로 읽을 stale 입력이 없습니다.");
    this.snapshot.recovery = null;
    this.snapshot.phase = "plan_ready";
    this.snapshot.project.runState = "idle";
    this.snapshot.project.activeBlocker = null;
    this.commit("project.snapshot_refreshed", "최신 State·Project Map과 Plan revision을 다시 읽었습니다.", "project", this.snapshot.project.projectId, context);
    return { ok: true, data: await this.getSnapshot() };
  }

  async observeAttempt(context: CommandContext): Promise<CommandResult> {
    if (this.snapshot.recovery?.kind !== "external_unknown") return this.error("NO_UNKNOWN_INTENT", "관측할 unknown intent가 없습니다.");
    this.snapshot.recovery.stage = "observed";
    this.snapshot.recovery.receiptState = "confirmed_missing";
    this.snapshot.recovery.explanation = "저장된 thread를 읽어 turn 중단과 receipt 부재를 확인했습니다. 새 thread 없이 마지막 checkpoint에서 재개할 수 있습니다.";
    this.snapshot.recovery.evidence = ["thread/read 완료", "기존 turn interrupted 확인", "후속 mutation 없음"];
    if (this.snapshot.activeAttempt) this.snapshot.activeAttempt.status = "interrupted";
    this.commit("attempt.observed", "저장 thread를 재개 없이 먼저 관측했습니다.", "attempt", this.snapshot.activeAttempt?.attemptId ?? "attempt_demo_02", context);
    return { ok: true, data: await this.getSnapshot() };
  }

  async resumeAttempt(context: CommandContext): Promise<CommandResult> {
    if (this.snapshot.recovery?.kind !== "external_unknown" || this.snapshot.recovery.stage !== "observed") {
      return this.error("OBSERVATION_REQUIRED", "resume 전에 기존 provider 상태를 관측해야 합니다.");
    }
    const task = this.snapshot.tasks.find((candidate) => candidate.taskId === this.snapshot.activeAttempt?.taskId);
    if (task) task.status = "running";
    if (this.snapshot.activeAttempt) this.snapshot.activeAttempt.status = "running";
    this.snapshot.recovery = null;
    this.snapshot.phase = "running";
    this.snapshot.project.runState = "active";
    this.snapshot.project.activeBlocker = null;
    this.commit("attempt.resumed", "기존 thread의 마지막 checkpoint에서 후속 turn을 시작했습니다.", "attempt", this.snapshot.activeAttempt?.attemptId ?? "attempt_demo_02", context);
    return { ok: true, data: await this.getSnapshot() };
  }

  async abandonIntent(rationale: string, context: CommandContext): Promise<CommandResult> {
    if (this.snapshot.recovery?.kind !== "external_unknown" || this.snapshot.recovery.stage !== "observed") {
      return this.error("OBSERVATION_REQUIRED", "intent를 포기하기 전에 기존 provider 상태를 관측해야 합니다.");
    }
    if (!rationale.trim()) return this.error("RATIONALE_REQUIRED", "감사 History에 남길 포기 사유가 필요합니다.");
    if (this.snapshot.activeAttempt) this.snapshot.activeAttempt.status = "abandoned";
    const task = this.snapshot.tasks.find((candidate) => candidate.taskId === this.snapshot.activeAttempt?.taskId);
    if (task) task.status = "blocked";
    const abandonedPlanId = this.snapshot.activePlanRevisionId;
    this.snapshot.activePlanRevisionId = null;
    this.snapshot.plans = this.snapshot.plans.map((plan) => plan.planRevisionId === abandonedPlanId ? { ...plan, decision: "needs_revision" } : plan);
    this.snapshot.recovery = null;
    this.snapshot.phase = "goal_ready";
    this.snapshot.project.runState = "idle";
    this.snapshot.project.activeBlocker = null;
    this.commit("runtime_intent.abandoned", `사용자 사유: ${rationale.trim()}`, "runtime_intent", "intent_demo_resume_02", context);
    return { ok: true, data: await this.getSnapshot() };
  }

  private async advanceGoalTest(context: CommandContext): Promise<CommandResult> {
    if (this.snapshot.validation.goalTest === "pending") {
      this.snapshot.validation.goalTest = "running";
      this.commit("goal_test.dispatched", "Task와 분리된 독립 Goal Test를 시작했습니다.", "goal_revision", this.snapshot.goal.revisionId, context, {
        action: "dispatched",
        lane: "goal_test",
        taskRef: null,
        detail: "독립 Goal Test를 별도 실행했습니다.",
      });
      return { ok: true, data: await this.getSnapshot() };
    }
    this.snapshot.validation.goalTest = "passed";
    this.snapshot.validation.verdict = "satisfied";
    this.snapshot.phase = "completed";
    this.snapshot.project.runState = "completed";
    this.snapshot.plans = this.snapshot.plans.map((plan) => plan.planRevisionId === this.snapshot.activePlanRevisionId ? { ...plan, decision: "selected" } : plan);
    this.commit("goal.satisfied", "독립 Goal Test와 모든 criterion evidence를 확인했습니다.", "goal_revision", this.snapshot.goal.revisionId, context, {
      action: "completed",
      lane: "goal_test",
      taskRef: null,
      detail: "GoalVerdict가 satisfied로 기록됐습니다.",
    });
    return { ok: true, data: await this.getSnapshot() };
  }

  private commit(
    eventType: string,
    summary: string,
    entityType: string,
    entityId: string,
    context: CommandContext,
    runLog?: Omit<RunLogEntry, "sequence">,
  ): void {
    const sequence = this.snapshot.historyCursor.sequence + 1;
    const event: HistoryEventView = {
      sequence,
      eventHash: eventHash(sequence),
      eventType,
      entityType,
      entityId,
      summary,
      actorRef: context.actorRef,
      source: context.clientInstanceId,
      occurredAt: now(),
    };
    this.snapshot.history = [...this.snapshot.history, event];
    this.snapshot.historyCursor = { sequence, eventHash: event.eventHash };
    this.snapshot.generatedAt = event.occurredAt;
    if (runLog) this.snapshot.runLog = [...this.snapshot.runLog, { ...runLog, sequence }];
    this.snapshot.possibleActions = possibleActionsFor(this.snapshot);
    this.emit();
  }

  private error(code: string, message: string, suggestedAction: PossibleActionKind | null = null): CommandResult {
    this.snapshot.possibleActions = possibleActionsFor(this.snapshot);
    this.emit();
    return {
      ok: false,
      error: {
        code,
        message,
        retryable: false,
        suggestedAction,
        historyCursor: structuredClone(this.snapshot.historyCursor),
      },
      data: structuredClone(this.snapshot),
    };
  }

  private emit(): void {
    const copy = structuredClone(this.snapshot);
    for (const listener of this.listeners) listener(copy);
  }
}

export { DIGESTS };
