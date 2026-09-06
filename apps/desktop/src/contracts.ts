export type ScenarioKey = "golden-path" | "stale-activation" | "external-unknown";

export type AppView = "overview" | "goal" | "plans" | "run" | "recovery";

export type WorkspacePhase =
  | "goal_draft"
  | "goal_ready"
  | "plan_ready"
  | "plan_active"
  | "running"
  | "recovery"
  | "completed";

export type RevisionStatus =
  | "draft"
  | "ready"
  | "active"
  | "superseded"
  | "completed"
  | "needs_input"
  | "conflict";

export type TaskRuntimeStatus =
  | "pending"
  | "ready"
  | "materialized"
  | "reserved"
  | "running"
  | "validating"
  | "completed"
  | "failed"
  | "blocked"
  | "superseded";

export type AttemptStatus =
  | "reserved"
  | "starting"
  | "running"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "unknown"
  | "abandoned";

export type RunOnceAction =
  | "materialized"
  | "dispatched"
  | "observed"
  | "validated"
  | "completed"
  | "blocked"
  | "idle";

export type PossibleActionKind =
  | "edit_goal"
  | "prepare_goal"
  | "search_plans"
  | "activate_plan"
  | "run_once"
  | "refresh_snapshot"
  | "observe_attempt"
  | "resume_attempt"
  | "abandon_intent";

export interface HistoryCursor {
  sequence: number;
  eventHash: string;
}

export interface CommandContext {
  actorRef: string;
  clientInstanceId: string;
  requestId: string;
  idempotencyKey: string;
}

export interface ExpectedState {
  historySequence: number;
  goalDigest?: string;
  planDigest?: string;
}

export interface PossibleAction {
  kind: PossibleActionKind;
  enabled: boolean;
  disabledReason: string | null;
  confirmation: "none" | "plan_activation" | "external_effect";
  checkpointRequired: boolean;
}

export interface ProjectSummary {
  projectId: string;
  name: string;
  location: {
    locationRef: string;
    displayName: string;
    displayPath: string;
  };
  runState: "idle" | "active" | "recovery_required" | "completed";
  activeBlocker: string | null;
}

export interface GoalCriterionView {
  criterionId: string;
  statement: string;
  validationIntent: string;
}

export interface GoalRevisionView {
  goalId: string;
  revisionId: string;
  revisionNo: number;
  status: RevisionStatus;
  sourceRequest: string;
  sourceRequestDigest: string;
  definitionDigest: string;
  observableOutcome: string;
  missionClass: string;
  criteria: GoalCriterionView[];
  constraints: string[];
  nonGoals: string[];
  effectPolicy: {
    mutation: string;
    behavior: string;
    prohibitedEffects: string[];
  };
  preparationBinding: {
    normalizationStatus: "pending" | "complete";
    independentReview: "pending" | "passed";
    reviewerRole: string;
  };
}

export interface PlanTaskPreview {
  taskRef: string;
  title: string;
  contributesTo: string[];
  dependencies: string[];
}

export interface PlanCandidateView {
  planRevisionId: string;
  revisionNo: number;
  name: string;
  summary: string;
  decision: "admissible" | "selected" | "needs_revision" | "blocked";
  score: number | null;
  activationDigest: string;
  definitionDigest: string;
  goalDigest: string;
  risk: "low" | "medium" | "high";
  checkpoint: string;
  modelEnvelope: string;
  tasks: PlanTaskPreview[];
  validationSummary: string;
}

export interface TaskView {
  taskId: string;
  taskRef: string;
  title: string;
  description: string;
  status: TaskRuntimeStatus;
  dependencies: string[];
  contributesTo: string[];
  executionSpecRevisionId: string | null;
  attemptId: string | null;
  updatedAt: string;
}

export interface ValidationLaneView {
  workerObservation: "pending" | "running" | "collected";
  taskValidation: "pending" | "running" | "passed" | "failed";
  goalTest: "pending" | "running" | "passed" | "failed";
  verdict: "unknown" | "satisfied" | "unsatisfied";
}

export interface AttemptView {
  attemptId: string;
  taskId: string;
  status: AttemptStatus;
  threadId: string | null;
  turnId: string | null;
  intentId: string;
  receiptId: string | null;
}

export interface RecoveryCaseView {
  kind: "stale_input" | "external_unknown";
  stage: "detected" | "observed";
  title: string;
  explanation: string;
  expectedDigest: string | null;
  currentDigest: string | null;
  intentId: string | null;
  receiptState: "not_applicable" | "missing" | "confirmed_missing";
  lastCheckpoint: string;
  evidence: string[];
}

export interface HistoryEventView {
  sequence: number;
  eventHash: string;
  eventType: string;
  entityType: string;
  entityId: string;
  summary: string;
  actorRef: string;
  source: string;
  occurredAt: string;
}

export interface BudgetSummaryView {
  inputTokens: number | null;
  cachedInputTokens: number | null;
  outputTokens: number | null;
  latencyMs: number | null;
  availabilityReason: string | null;
}

export interface RunLogEntry {
  action: RunOnceAction;
  lane: "worker" | "task_validation" | "goal_test";
  taskRef: string | null;
  detail: string;
  sequence: number;
}

export interface WorkspaceSnapshot {
  apiVersion: "1";
  engineSchemaVersion: 2;
  productStatus: "NO-GO";
  generatedAt: string;
  scenario: ScenarioKey;
  phase: WorkspacePhase;
  project: ProjectSummary;
  goal: GoalRevisionView;
  plans: PlanCandidateView[];
  recommendedPlanRevisionId: string | null;
  activePlanRevisionId: string | null;
  tasks: TaskView[];
  validation: ValidationLaneView;
  activeAttempt: AttemptView | null;
  recovery: RecoveryCaseView | null;
  history: HistoryEventView[];
  historyCursor: HistoryCursor;
  budget: BudgetSummaryView;
  possibleActions: PossibleAction[];
  runLog: RunLogEntry[];
}

export interface CommandError {
  code: string;
  message: string;
  retryable: boolean;
  suggestedAction: PossibleActionKind | null;
  historyCursor: HistoryCursor;
}

export type CommandResult<T = WorkspaceSnapshot> =
  | { ok: true; data: T }
  | { ok: false; error: CommandError; data: WorkspaceSnapshot };

export interface ActivatePlanCommand {
  planRevisionId: string;
  activationDigest: string;
  expectedGoalDigest: string;
  expectedState: ExpectedState;
}

export interface EngineClient {
  getSnapshot(): Promise<WorkspaceSnapshot>;
  subscribe(listener: (snapshot: WorkspaceSnapshot) => void): () => void;
  resetScenario(scenario: ScenarioKey): Promise<WorkspaceSnapshot>;
  startGoalRevision(context: CommandContext): Promise<WorkspaceSnapshot>;
  prepareGoal(sourceRequest: string, context: CommandContext): Promise<CommandResult>;
  searchPlans(context: CommandContext): Promise<CommandResult>;
  activatePlan(command: ActivatePlanCommand, context: CommandContext): Promise<CommandResult>;
  runOnce(context: CommandContext): Promise<CommandResult>;
  refreshStale(context: CommandContext): Promise<CommandResult>;
  observeAttempt(context: CommandContext): Promise<CommandResult>;
  resumeAttempt(context: CommandContext): Promise<CommandResult>;
  abandonIntent(rationale: string, context: CommandContext): Promise<CommandResult>;
}

export interface PlatformCapabilities {
  os: "browser-host" | "windows" | "macos" | "linux";
  architecture: string;
  shell: "browser" | "tauri";
  runtimeAvailable: boolean;
  filePickerAvailable: boolean;
  backgroundExecutionAvailable: boolean;
}

export interface PlatformAdapter {
  getCapabilities(): PlatformCapabilities;
  selectProjectDirectory(): Promise<ProjectSummary["location"] | null>;
}
