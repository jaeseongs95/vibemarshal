import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  CircleDollarSign,
  FileCheck2,
  GitCompareArrows,
  Play,
  Radar,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";

import {
  EmptyState,
  Modal,
  PageIntro,
  StatusBadge,
  TaskDag,
  TaskRunway,
  ValidationTrace,
  shortDigest,
} from "./components";
import type { ActivatePlanCommand, AppView, PlanCandidateView, PossibleActionKind, WorkspaceSnapshot } from "./contracts";
import { t } from "./messages";

function hasAction(snapshot: WorkspaceSnapshot, kind: PossibleActionKind): boolean {
  return snapshot.possibleActions.some((action) => action.kind === kind && action.enabled);
}

const primaryLabels: Partial<Record<PossibleActionKind, string>> = {
  edit_goal: t("goal.newRevision"),
  prepare_goal: t("goal.normalize"),
  search_plans: t("goal.searchPlans"),
  activate_plan: t("overview.openPlans"),
  run_once: t("overview.runOnce"),
  refresh_snapshot: t("recovery.refresh"),
  observe_attempt: t("recovery.observe"),
  resume_attempt: t("recovery.resume"),
  abandon_intent: t("recovery.abandon"),
};

interface OverviewProps {
  snapshot: WorkspaceSnapshot;
  onNavigate: (view: AppView) => void;
  onAction: (kind: PossibleActionKind) => void;
}

export function OverviewScreen({ snapshot, onNavigate, onAction }: OverviewProps) {
  const primary = snapshot.possibleActions.find((action) => action.enabled);
  let title = t("overview.planReadyTitle");
  let description = t("overview.planReadyDescription");
  if (snapshot.recovery) {
    title = t("overview.recoveryTitle");
    description = snapshot.recovery.explanation;
  } else if (["plan_active", "running"].includes(snapshot.phase)) {
    title = t("overview.activeTitle");
    description = t("overview.activeDescription");
  } else if (snapshot.phase === "completed") {
    title = t("overview.completedTitle");
    description = t("overview.completedDescription");
  } else if (["goal_draft", "goal_ready"].includes(snapshot.phase)) {
    title = snapshot.phase === "goal_draft" ? "요청을 Goal Contract로 준비하세요" : "검토된 Goal에서 Plan 후보를 만드세요";
    description = "다음 단계는 Core가 반환한 possible_actions에서 가져옵니다.";
  }

  const activePlan = snapshot.plans.find((plan) => plan.planRevisionId === snapshot.activePlanRevisionId);
  const completed = snapshot.tasks.filter((task) => task.status === "completed").length;
  return (
    <>
      <PageIntro eyebrow={t("overview.eyebrow")} title={t("overview.title")} cursor={snapshot.historyCursor.sequence} />
      <section className="next-action" aria-labelledby="next-action-title">
        <div className="signal-orbit" aria-hidden="true">{snapshot.recovery ? <ShieldAlert size={23} /> : <Radar size={23} />}</div>
        <div className="next-action-copy">
          <p className="eyebrow">{t("overview.next")}</p>
          <h2 id="next-action-title">{title}</h2>
          <p>{description}</p>
        </div>
        {primary ? (
          <button className="primary-action" type="button" onClick={() => onAction(primary.kind)}>
            {primaryLabels[primary.kind]} <ArrowRight size={17} />
          </button>
        ) : null}
      </section>

      <div className="overview-grid">
        <section className="surface task-surface" aria-labelledby="task-heading">
          <div className="surface-heading">
            <div><p className="eyebrow">{t("overview.activePlan")}</p><h2 id="task-heading">{t("overview.taskFlow")}</h2></div>
            <span className="revision-label">{activePlan ? `revision ${activePlan.revisionNo}` : "비활성"}</span>
          </div>
          <TaskRunway tasks={snapshot.tasks} />
        </section>

        <section className="surface trace-surface" aria-labelledby="trace-heading">
          <div className="surface-heading">
            <div><p className="eyebrow">{t("overview.validationTrace")}</p><h2 id="trace-heading">{t("overview.validationBoundary")}</h2></div>
            <ShieldCheck size={20} />
          </div>
          <ValidationTrace validation={snapshot.validation} />
        </section>
      </div>

      <div className="overview-summary-grid">
        <button className="surface summary-card" type="button" onClick={() => onNavigate("run")}>
          <span className="summary-icon"><FileCheck2 size={18} /></span>
          <span><small>{t("run.progress")}</small><strong>{snapshot.tasks.length ? `${completed} / ${snapshot.tasks.length}` : "Plan 미활성"}</strong></span>
        </button>
        <button className="surface summary-card" type="button" onClick={() => onNavigate("plans")}>
          <span className="summary-icon"><GitCompareArrows size={18} /></span>
          <span><small>Plan 후보</small><strong>{snapshot.plans.length}개 · {snapshot.activePlanRevisionId ? "활성화됨" : "비권위"}</strong></span>
        </button>
        <div className="surface summary-card">
          <span className="summary-icon"><CircleDollarSign size={18} /></span>
          <span><small>{t("overview.budget")}</small><strong>{snapshot.budget.inputTokens?.toLocaleString() ?? "알 수 없음"} input</strong></span>
        </div>
      </div>
    </>
  );
}

interface GoalProps {
  snapshot: WorkspaceSnapshot;
  onNewRevision: () => void;
  onPrepare: (source: string) => void;
  onSearchPlans: () => void;
}

export function GoalScreen({ snapshot, onNewRevision, onPrepare, onSearchPlans }: GoalProps) {
  const [source, setSource] = useState(snapshot.goal.sourceRequest);
  useEffect(() => setSource(snapshot.goal.sourceRequest), [snapshot.goal.revisionId, snapshot.goal.sourceRequest]);
  const isDraft = snapshot.goal.status === "draft";
  return (
    <>
      <PageIntro eyebrow={t("goal.eyebrow")} title={t("goal.title")} description={t("goal.description")} cursor={snapshot.historyCursor.sequence} />
      <section className="surface contract-editor">
        <div className="surface-heading contract-heading">
          <div>
            <span className="revision-kicker">{snapshot.goal.revisionId}</span>
            <h2>{t("goal.sourceLabel")}</h2>
          </div>
          <StatusBadge status={snapshot.goal.status} />
        </div>
        <div className="contract-editor-body">
          <label className="field-label" htmlFor="goal-source">{t("goal.sourceLabel")}</label>
          <textarea id="goal-source" value={source} onChange={(event) => setSource(event.target.value)} readOnly={!isDraft} rows={5} />
          <div className="button-row">
            {hasAction(snapshot, "edit_goal") ? <button className="secondary-action" type="button" onClick={onNewRevision}><RotateCcw size={16} /> {t("goal.newRevision")}</button> : null}
            {hasAction(snapshot, "prepare_goal") ? <button className="primary-action" type="button" onClick={() => onPrepare(source)} disabled={!source.trim()}>{t("goal.normalize")} <ArrowRight size={16} /></button> : null}
            {hasAction(snapshot, "search_plans") ? <button className="primary-action" type="button" onClick={onSearchPlans}>{t("goal.searchPlans")} <Search size={16} /></button> : null}
          </div>
        </div>
      </section>

      <div className="contract-grid">
        <section className="surface contract-card contract-card--wide">
          <p className="eyebrow">{t("goal.outcome")}</p>
          <h2>{snapshot.goal.observableOutcome}</h2>
          <code title={snapshot.goal.definitionDigest}>{shortDigest(snapshot.goal.definitionDigest)}</code>
        </section>
        <section className="surface contract-card">
          <p className="eyebrow">{t("goal.review")}</p>
          <div className="review-status"><CheckCircle2 size={21} /><strong>{snapshot.goal.preparationBinding.independentReview === "passed" ? t("goal.reviewPassed") : "검토 대기"}</strong></div>
          <small>{snapshot.goal.preparationBinding.reviewerRole}</small>
        </section>
        <section className="surface contract-card contract-card--wide">
          <p className="eyebrow">{t("goal.hardAc")}</p>
          <ol className="criteria-list">
            {snapshot.goal.criteria.map((criterion) => (
              <li key={criterion.criterionId}>
                <span>{criterion.criterionId}</span>
                <div><strong>{criterion.statement}</strong><small>{criterion.validationIntent}</small></div>
              </li>
            ))}
          </ol>
        </section>
        <section className="surface contract-card">
          <p className="eyebrow">{t("goal.constraints")}</p>
          <ul className="plain-list">{snapshot.goal.constraints.map((item) => <li key={item}>{item}</li>)}</ul>
          <p className="eyebrow subsection-label">{t("goal.nonGoals")}</p>
          <ul className="plain-list">{snapshot.goal.nonGoals.map((item) => <li key={item}>{item}</li>)}</ul>
        </section>
      </div>
    </>
  );
}

interface PlansProps {
  snapshot: WorkspaceSnapshot;
  selectedPlanId: string | null;
  onSelect: (planId: string) => void;
  onActivate: (command: ActivatePlanCommand) => void;
  onNavigate: (view: AppView) => void;
}

export function PlansScreen({ snapshot, selectedPlanId, onSelect, onActivate, onNavigate }: PlansProps) {
  const [modalOpen, setModalOpen] = useState(false);
  const selected = snapshot.plans.find((plan) => plan.planRevisionId === selectedPlanId) ?? snapshot.plans[0];
  const command = selected ? {
    planRevisionId: selected.planRevisionId,
    activationDigest: selected.activationDigest,
    expectedGoalDigest: snapshot.goal.definitionDigest,
    expectedState: {
      historySequence: snapshot.historyCursor.sequence,
      goalDigest: snapshot.goal.definitionDigest,
      planDigest: selected.activationDigest,
    },
  } satisfies ActivatePlanCommand : null;

  const confirm = () => {
    if (!command) return;
    setModalOpen(false);
    onActivate(command);
  };
  return (
    <>
      <PageIntro eyebrow={t("plans.eyebrow")} title={t("plans.title")} description={t("plans.description")} cursor={snapshot.historyCursor.sequence} />
      {!snapshot.plans.length ? (
        <EmptyState title="준비된 Plan 후보가 없습니다" description="Goal을 정규화하고 독립 검토한 뒤 후보를 탐색하세요.">
          <button className="secondary-action" type="button" onClick={() => onNavigate("goal")}>Goal로 이동</button>
        </EmptyState>
      ) : (
        <>
          {hasAction(snapshot, "activate_plan") && selected ? (
            <div className="sticky-action-bar">
              <div><small>선택한 후보</small><strong>{selected.name}</strong></div>
              <button className="primary-action" type="button" onClick={() => setModalOpen(true)}>{t("plans.activate")} <ArrowRight size={17} /></button>
            </div>
          ) : null}
          <div className="plan-grid">
            {snapshot.plans.map((plan) => (
              <PlanCard key={plan.planRevisionId} plan={plan} recommended={plan.planRevisionId === snapshot.recommendedPlanRevisionId} selected={plan.planRevisionId === selected?.planRevisionId} active={plan.planRevisionId === snapshot.activePlanRevisionId} onSelect={() => onSelect(plan.planRevisionId)} />
            ))}
          </div>
        </>
      )}
      <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={t("activate.title")} description={t("activate.description")}>
        {selected && command ? (
          <>
            <dl className="confirmation-list">
              <div><dt>{t("activate.revision")}</dt><dd>{selected.planRevisionId}</dd></div>
              <div><dt>{t("activate.digest")}</dt><dd><code>{selected.activationDigest}</code></dd></div>
              <div><dt>{t("activate.goalDigest")}</dt><dd><code>{snapshot.goal.definitionDigest}</code></dd></div>
              <div><dt>{t("activate.cursor")}</dt><dd>sequence #{command.expectedState.historySequence}</dd></div>
              <div><dt>{t("activate.effects")}</dt><dd>Task DAG {selected.tasks.length}개 · {selected.checkpoint} · {selected.validationSummary}</dd></div>
            </dl>
            <div className="modal-actions">
              <button className="secondary-action" type="button" onClick={() => setModalOpen(false)}>{t("common.back")}</button>
              <button className="primary-action" type="button" onClick={confirm}>{t("activate.confirm")} <ShieldCheck size={17} /></button>
            </div>
          </>
        ) : null}
      </Modal>
    </>
  );
}

function PlanCard({ plan, recommended, selected, active, onSelect }: { plan: PlanCandidateView; recommended: boolean; selected: boolean; active: boolean; onSelect: () => void }) {
  const riskLabel = { low: "낮음", medium: "중간", high: "높음" }[plan.risk];
  return (
    <article className={`surface plan-card ${selected ? "plan-card--selected" : ""}`}>
      <div className="plan-card-heading">
        <div className="badge-row">
          <span className="candidate-badge">{t("plans.candidate")}</span>
          {recommended ? <span className="recommended-badge">{t("plans.recommended")}</span> : null}
          {active ? <span className="active-badge">{t("plans.active")}</span> : null}
        </div>
        <StatusBadge status={plan.decision} />
      </div>
      <h2>{plan.name}</h2>
      <p className="plan-summary">{plan.summary}</p>
      <TaskDag plan={plan} />
      <dl className="plan-meta">
        <div><dt>{t("plans.score")}</dt><dd>{plan.score ?? "—"}</dd></div>
        <div><dt>{t("plans.risk")}</dt><dd>{riskLabel}</dd></div>
        <div><dt>{t("plans.validation")}</dt><dd>{plan.validationSummary}</dd></div>
        <div><dt>{t("plans.model")}</dt><dd>{plan.modelEnvelope}</dd></div>
        <div><dt>Revision</dt><dd>{plan.planRevisionId}</dd></div>
        <div><dt>Digest</dt><dd title={plan.activationDigest}>{shortDigest(plan.activationDigest)}</dd></div>
      </dl>
      <button className={selected ? "selected-plan-button" : "secondary-action"} type="button" onClick={onSelect} aria-pressed={selected}>
        {selected ? <><CheckCircle2 size={16} /> 선택됨</> : t("plans.select")}
      </button>
    </article>
  );
}

interface RunProps {
  snapshot: WorkspaceSnapshot;
  onRunOnce: () => void;
  onNavigate: (view: AppView) => void;
}

export function RunScreen({ snapshot, onRunOnce, onNavigate }: RunProps) {
  const activePlan = snapshot.plans.find((plan) => plan.planRevisionId === snapshot.activePlanRevisionId);
  const completed = snapshot.tasks.filter((task) => task.status === "completed").length;
  const current = snapshot.tasks.find((task) => task.status !== "completed");
  return (
    <>
      <PageIntro eyebrow={t("run.eyebrow")} title={t("run.title")} description={t("run.description")} cursor={snapshot.historyCursor.sequence} />
      {!activePlan ? (
        <EmptyState title={t("run.noPlan")} description="실행은 exact revision과 activation digest가 원장에 결속된 뒤에만 가능합니다.">
          <button className="secondary-action" type="button" onClick={() => onNavigate("plans")}>{t("run.backToPlans")}</button>
        </EmptyState>
      ) : (
        <>
          <section className="surface run-command-bar">
            <div>
              <p className="eyebrow">{current ? `NEXT · ${current.taskRef}` : "PLAN-LEVEL GOAL TEST"}</p>
              <h2>{current?.title ?? (snapshot.validation.goalTest === "pending" ? "독립 Goal Test 시작" : "최종 GoalVerdict 기록")}</h2>
              <p>{activePlan.planRevisionId} · {completed}/{snapshot.tasks.length} Task 완료</p>
            </div>
            {hasAction(snapshot, "run_once") ? <button className="primary-action" type="button" onClick={onRunOnce}>{t("run.nextStep")} <Play size={16} /></button> : <StatusBadge status={snapshot.validation.verdict} />}
          </section>
          <div className="run-grid">
            <section className="surface">
              <div className="surface-heading"><div><p className="eyebrow">TASK LEDGER</p><h2>{t("run.progress")}</h2></div><span className="revision-label">{completed}/{snapshot.tasks.length}</span></div>
              <TaskRunway tasks={snapshot.tasks} />
            </section>
            <section className="surface trace-surface">
              <div className="surface-heading"><div><p className="eyebrow">THREE LANES</p><h2>{t("overview.validationBoundary")}</h2></div><ShieldCheck size={20} /></div>
              <ValidationTrace validation={snapshot.validation} />
            </section>
          </div>
          <div className="run-grid run-grid--lower">
            <section className="surface detail-card">
              <p className="eyebrow">{t("run.currentAttempt")}</p>
              {snapshot.activeAttempt ? (
                <dl className="detail-list">
                  <div><dt>Attempt</dt><dd>{snapshot.activeAttempt.attemptId}</dd></div>
                  <div><dt>상태</dt><dd><StatusBadge status={snapshot.activeAttempt.status} /></dd></div>
                  <div><dt>Thread</dt><dd>{snapshot.activeAttempt.threadId ?? "—"}</dd></div>
                  <div><dt>Receipt</dt><dd>{snapshot.activeAttempt.receiptId ?? "없음"}</dd></div>
                </dl>
              ) : <p className="muted-copy">활성 Attempt가 없습니다. materialize와 dispatch는 서로 다른 run once 단계입니다.</p>}
            </section>
            <section className="surface detail-card">
              <p className="eyebrow">{t("run.timeline")}</p>
              {snapshot.runLog.length ? (
                <ol className="timeline-list">
                  {snapshot.runLog.slice().reverse().map((entry) => <li key={entry.sequence}><span>#{entry.sequence}</span><div><strong>{entry.taskRef ? `${entry.taskRef} · ` : ""}{entry.action}</strong><small>{entry.detail}</small></div></li>)}
                </ol>
              ) : <p className="muted-copy">{t("run.noTimeline")}</p>}
            </section>
          </div>
        </>
      )}
    </>
  );
}

interface RecoveryProps {
  snapshot: WorkspaceSnapshot;
  onRefresh: () => void;
  onObserve: () => void;
  onResume: () => void;
  onAbandon: (rationale: string) => void;
  onNavigate: (view: AppView) => void;
}

export function RecoveryScreen({ snapshot, onRefresh, onObserve, onResume, onAbandon, onNavigate }: RecoveryProps) {
  const [rationale, setRationale] = useState("");
  const recovery = snapshot.recovery;
  const receiptLabel = useMemo(() => ({ not_applicable: "해당 없음", missing: "미확인", confirmed_missing: "부재 확인" })[recovery?.receiptState ?? "not_applicable"], [recovery]);
  return (
    <>
      <PageIntro eyebrow={t("recovery.eyebrow")} title={t("recovery.title")} description={t("recovery.description")} cursor={snapshot.historyCursor.sequence} />
      {!recovery ? (
        <EmptyState title={t("recovery.none")} description={t("recovery.noneDescription")}>
          <button className="secondary-action" type="button" onClick={() => onNavigate(snapshot.activePlanRevisionId ? "run" : "overview")}>현재 흐름으로 돌아가기</button>
        </EmptyState>
      ) : (
        <section className="surface recovery-card" aria-live="polite">
          <div className={`recovery-signal recovery-signal--${recovery.kind === "stale_input" ? "stale" : "unknown"}`}>
            {recovery.kind === "stale_input" ? <RefreshCw size={22} /> : <AlertTriangle size={22} />}
          </div>
          <div className="recovery-body">
            <div className="badge-row">
              <span className="warning-badge">{recovery.kind === "stale_input" ? "STALE_EXECUTION_INPUT" : "EXTERNAL_UNKNOWN"}</span>
              <StatusBadge status={recovery.stage} />
            </div>
            <h2>{recovery.title}</h2>
            <p className="recovery-explanation">{recovery.explanation}</p>
            <dl className="recovery-details">
              <div><dt>{t("recovery.expected")}</dt><dd><code>{recovery.expectedDigest ?? "—"}</code></dd></div>
              <div><dt>{t("recovery.current")}</dt><dd><code>{recovery.currentDigest ?? "—"}</code></dd></div>
              <div><dt>{t("recovery.intent")}</dt><dd>{recovery.intentId ?? "provider 호출 전"}</dd></div>
              <div><dt>{t("recovery.receipt")}</dt><dd>{receiptLabel}</dd></div>
              <div><dt>{t("recovery.checkpoint")}</dt><dd>{recovery.lastCheckpoint}</dd></div>
            </dl>
            <div className="evidence-box">
              <strong>{t("recovery.evidence")}</strong>
              <ul>{recovery.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
            {recovery.kind === "stale_input" ? (
              <div className="button-row"><button className="primary-action" type="button" onClick={onRefresh}>{t("recovery.refresh")} <RefreshCw size={16} /></button></div>
            ) : recovery.stage === "detected" ? (
              <div className="recovery-action-copy">
                <p><strong>재실행 버튼이 없는 이유</strong><br />결과를 모르는 intent는 자동 재시도하면 중복 효과가 생길 수 있습니다.</p>
                <button className="primary-action" type="button" onClick={onObserve}>{t("recovery.observe")} <Search size={16} /></button>
              </div>
            ) : (
              <div className="observed-actions">
                <div className="safe-resume">
                  <strong>기존 thread에서 계속</strong>
                  <p>새 Task나 thread를 만들지 않고 확인된 마지막 checkpoint에서 이어갑니다.</p>
                  <button className="primary-action" type="button" onClick={onResume}>{t("recovery.resume")} <Play size={16} /></button>
                </div>
                <form className="abandon-form" onSubmit={(event) => { event.preventDefault(); onAbandon(rationale); }}>
                  <label className="field-label" htmlFor="abandon-rationale">{t("recovery.rationale")}</label>
                  <textarea id="abandon-rationale" value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder={t("recovery.rationalePlaceholder")} rows={3} />
                  <button className="danger-action" type="submit" disabled={!rationale.trim()}>{t("recovery.confirmAbandon")}</button>
                </form>
              </div>
            )}
          </div>
        </section>
      )}
    </>
  );
}
