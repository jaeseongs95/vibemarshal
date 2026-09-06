import { useEffect, useRef, type ReactNode } from "react";
import {
  Activity,
  Check,
  CircleDot,
  FolderKanban,
  GitBranch,
  History,
  LockKeyhole,
  Menu,
  PanelRightOpen,
  ShieldCheck,
  X,
} from "lucide-react";

import type {
  AppView,
  PlanCandidateView,
  PlatformCapabilities,
  ScenarioKey,
  TaskRuntimeStatus,
  TaskView,
  ValidationLaneView,
  WorkspaceSnapshot,
} from "./contracts";
import { t } from "./messages";

const viewLabels: Record<AppView, string> = {
  overview: t("nav.overview"),
  goal: t("nav.goal"),
  plans: t("nav.plans"),
  run: t("nav.run"),
  recovery: t("nav.recovery"),
};

const taskLabels: Record<TaskRuntimeStatus, string> = {
  pending: "대기",
  ready: "준비",
  materialized: "명세 고정",
  reserved: "예약",
  running: "실행 중",
  validating: "검증 중",
  completed: "완료",
  failed: "실패",
  blocked: "차단",
  superseded: "대체됨",
};

const statusLabels: Record<string, string> = {
  ...taskLabels,
  active: "활성",
  draft: "초안",
  passed: "통과",
  collected: "수집됨",
  satisfied: "충족",
  unsatisfied: "미충족",
  selected: "선택됨",
  admissible: "Gate 통과",
  needs_revision: "수정 필요",
  conflict: "충돌",
  needs_input: "입력 필요",
  unknown: "결과 불명",
  interrupted: "중단됨",
  abandoned: "포기됨",
  detected: "감지됨",
  observed: "관측됨",
};

function taskTone(status: TaskRuntimeStatus): string {
  if (status === "completed") return "done";
  if (["ready", "materialized", "reserved", "running", "validating"].includes(status)) return "ready";
  if (["failed", "blocked"].includes(status)) return "danger";
  return "pending";
}

export function shortDigest(value: string | null | undefined): string {
  if (!value) return "—";
  if (value.length <= 22) return value;
  return `${value.slice(0, 14)}…${value.slice(-7)}`;
}

interface HeaderProps {
  scenario: ScenarioKey;
  productStatus: WorkspaceSnapshot["productStatus"];
  onScenarioChange: (scenario: ScenarioKey) => void;
  onOpenProjects: () => void;
  onOpenInspector: () => void;
}

export function Header({ scenario, productStatus, onScenarioChange, onOpenProjects, onOpenInspector }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="topbar-start">
        <button className="icon-button mobile-only" type="button" onClick={onOpenProjects} aria-label={t("nav.openProjects")}>
          <Menu size={19} />
        </button>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">V</span>
          <span>{t("app.brand")}</span>
          <span className="prototype-label">{t("app.prototype")}</span>
        </div>
      </div>
      <div className="system-strip" aria-label="제품 및 런타임 상태">
        <label className="scenario-picker">
          <span>{t("app.scenario")}</span>
          <select value={scenario} onChange={(event) => onScenarioChange(event.target.value as ScenarioKey)}>
            <option value="golden-path">{t("scenario.golden")}</option>
            <option value="stale-activation">{t("scenario.stale")}</option>
            <option value="external-unknown">{t("scenario.external")}</option>
          </select>
        </label>
        <span className="status-chip status-chip--warning">Engine {productStatus}</span>
        <span className="status-chip runtime-chip"><CircleDot size={14} /> {t("app.runtimeMock")}</span>
        <button className="icon-button inspector-trigger" type="button" onClick={onOpenInspector} aria-label={t("nav.openInspector")}>
          <PanelRightOpen size={18} />
        </button>
      </div>
    </header>
  );
}

interface ProjectRailProps {
  snapshot: WorkspaceSnapshot;
  open: boolean;
  onClose: () => void;
}

export function ProjectRail({ snapshot, open, onClose }: ProjectRailProps) {
  const progress = snapshot.tasks.length
    ? `${snapshot.tasks.filter((task) => task.status === "completed").length}/${snapshot.tasks.length} 완료`
    : "활성 Plan 없음";
  return (
    <aside className={`project-rail ${open ? "is-drawer-open" : ""}`} aria-label="프로젝트">
      <div className="rail-heading">
        <span>{t("app.projects")}</span>
        <button className="icon-button drawer-close" type="button" onClick={onClose} aria-label={t("nav.close")}><X size={18} /></button>
      </div>
      <button className="project-item project-item--active" type="button" onClick={onClose} aria-current="true">
        <span className="project-glyph"><FolderKanban size={18} /></span>
        <span><strong>{snapshot.project.name}</strong><small>{snapshot.project.runState === "recovery_required" ? "복구 필요" : progress}</small></span>
      </button>
      <button className="project-item" type="button" disabled>
        <span className="project-glyph"><FolderKanban size={18} /></span>
        <span><strong>Context Continuity</strong><small>이 데모에서는 읽기 전용</small></span>
      </button>
      <div className="rail-footnote">
        <LockKeyhole size={15} />
        <span>{t("app.localWriter")}</span>
      </div>
    </aside>
  );
}

interface SectionTabsProps {
  view: AppView;
  onNavigate: (view: AppView) => void;
}

export function SectionTabs({ view, onNavigate }: SectionTabsProps) {
  return (
    <nav className="section-tabs" aria-label="프로젝트 화면">
      {(Object.keys(viewLabels) as AppView[]).map((item) => (
        <button
          key={item}
          className={view === item ? "is-active" : ""}
          type="button"
          onClick={() => onNavigate(item)}
          aria-current={view === item ? "page" : undefined}
        >
          {viewLabels[item]}
        </button>
      ))}
    </nav>
  );
}

interface PageIntroProps {
  eyebrow: string;
  title: string;
  description?: string;
  cursor: number;
}

export function PageIntro({ eyebrow, title, description, cursor }: PageIntroProps) {
  return (
    <section className="page-intro">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {description ? <p className="page-description">{description}</p> : null}
      </div>
      <span className="history-cursor"><History size={15} /> History #{cursor}</span>
    </section>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const positive = ["active", "completed", "passed", "collected", "satisfied", "selected", "admissible", "observed"].includes(status);
  const attention = ["ready", "materialized", "reserved", "running", "validating", "draft", "unknown", "interrupted", "detected"].includes(status);
  const negative = ["failed", "blocked", "unsatisfied", "conflict", "needs_input", "abandoned"].includes(status);
  const tone = positive ? "positive" : negative ? "negative" : attention ? "attention" : "neutral";
  return <span className={`state-badge state-badge--${tone}`}>{statusLabels[status] ?? status}</span>;
}

export function TaskRunway({ tasks, emptyTitle, emptyDescription }: { tasks: TaskView[]; emptyTitle?: string; emptyDescription?: string }) {
  if (!tasks.length) {
    return <EmptyState title={emptyTitle ?? t("overview.noActivePlan")} description={emptyDescription ?? t("overview.noActivePlanDescription")} compact />;
  }
  return (
    <div className="task-runway">
      {tasks.map((task, index) => {
        const tone = taskTone(task.status);
        return (
          <div className={`task-row task-row--${tone}`} key={task.taskId}>
            <div className="task-index" aria-hidden="true">{task.status === "completed" ? <Check size={15} /> : index + 1}</div>
            <div className="task-copy">
              <strong>{task.taskRef} · {task.title}</strong>
              <span>{task.executionSpecRevisionId ?? (task.dependencies.length ? `선행 ${task.dependencies.join(", ")}` : "dependency 없음")}</span>
            </div>
            <StatusBadge status={task.status} />
          </div>
        );
      })}
    </div>
  );
}

function validationLabel(value: string): string {
  return ({
    pending: t("common.pending"),
    running: t("common.running"),
    passed: t("common.passed"),
    collected: t("common.collected"),
    failed: "실패",
    unknown: t("common.unknown"),
    satisfied: "충족",
    unsatisfied: "미충족",
  } as Record<string, string>)[value] ?? value;
}

export function ValidationTrace({ validation }: { validation: ValidationLaneView }) {
  const rows = [
    [t("overview.workerObservation"), validation.workerObservation],
    [t("overview.taskValidation"), validation.taskValidation],
    [t("overview.goalTest"), validation.goalTest],
  ];
  return (
    <>
      {rows.map(([label, value], index) => (
        <div key={label}>
          <div className={`trace-step ${value === "pending" ? "trace-step--pending" : ""}`}>
            <span>{label}</span><strong>{validationLabel(value)}</strong>
          </div>
          {index < rows.length - 1 ? <div className={`trace-line ${value === "pending" ? "trace-line--muted" : ""}`} /> : null}
        </div>
      ))}
      <p className="surface-note">{t("overview.workerDisclaimer")}</p>
    </>
  );
}

export function TaskDag({ plan }: { plan: PlanCandidateView }) {
  const width = 680;
  const top = 28;
  const nodeWidth = 128;
  const nodeHeight = 64;
  const gap = (width - nodeWidth * plan.tasks.length) / Math.max(plan.tasks.length + 1, 2);
  const positions = new Map(plan.tasks.map((task, index) => [task.taskRef, { x: gap + index * (nodeWidth + gap), y: top }]));
  return (
    <div className="dag-wrap">
      <svg viewBox={`0 0 ${width} 120`} role="img" aria-labelledby={`dag-title-${plan.planRevisionId} dag-desc-${plan.planRevisionId}`}>
        <title id={`dag-title-${plan.planRevisionId}`}>{plan.name} Task DAG</title>
        <desc id={`dag-desc-${plan.planRevisionId}`}>{plan.tasks.map((task) => `${task.taskRef} ${task.title}, dependency ${task.dependencies.join(", ") || "없음"}`).join(". ")}</desc>
        <defs>
          <marker id={`arrow-${plan.planRevisionId}`} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
            <path d="M0,0 L7,3.5 L0,7 Z" className="dag-arrow" />
          </marker>
        </defs>
        {plan.tasks.flatMap((task) => task.dependencies.map((dependency) => {
          const from = positions.get(dependency);
          const to = positions.get(task.taskRef);
          if (!from || !to) return null;
          return <line key={`${dependency}-${task.taskRef}`} x1={from.x + nodeWidth} y1={from.y + nodeHeight / 2} x2={to.x - 7} y2={to.y + nodeHeight / 2} className="dag-line" markerEnd={`url(#arrow-${plan.planRevisionId})`} />;
        }))}
        {plan.tasks.map((task) => {
          const position = positions.get(task.taskRef)!;
          return (
            <g key={task.taskRef}>
              <rect x={position.x} y={position.y} width={nodeWidth} height={nodeHeight} rx="10" className="dag-node" />
              <text x={position.x + 12} y={position.y + 24} className="dag-ref">{task.taskRef}</text>
              <text x={position.x + 12} y={position.y + 46} className="dag-title">{task.title.length > 10 ? `${task.title.slice(0, 10)}…` : task.title}</text>
            </g>
          );
        })}
      </svg>
      <ol className="sr-only">
        {plan.tasks.map((task) => <li key={task.taskRef}>{task.taskRef} {task.title}. 선행 Task: {task.dependencies.join(", ") || "없음"}</li>)}
      </ol>
    </div>
  );
}

interface AuditPanelProps {
  snapshot: WorkspaceSnapshot;
  platform: PlatformCapabilities;
  open: boolean;
  onClose: () => void;
}

export function AuditPanel({ snapshot, platform, open, onClose }: AuditPanelProps) {
  const activePlan = snapshot.plans.find((plan) => plan.planRevisionId === snapshot.activePlanRevisionId);
  const recent = snapshot.history.at(-1);
  return (
    <aside className={`audit-panel ${open ? "is-drawer-open" : ""}`} aria-label="상태 및 감사 정보">
      <div className="inspector-title">
        <Activity size={17} /><strong>{t("audit.title")}</strong>
        <button className="icon-button drawer-close" type="button" onClick={onClose} aria-label={t("nav.close")}><X size={18} /></button>
      </div>
      <dl className="audit-list">
        <div><dt>{t("audit.api")}</dt><dd>호환됨 · v{snapshot.apiVersion}</dd></div>
        <div><dt>{t("audit.permission")}</dt><dd className="ok-text">{t("audit.permissionValue")}</dd></div>
        <div><dt>{t("audit.model")}</dt><dd>{t("audit.modelValue")}</dd></div>
        <div><dt>{t("audit.history")}</dt><dd className="ok-text">{t("audit.historyValue")}</dd></div>
      </dl>
      <hr />
      <div className="inspector-block">
        <span className="inspector-label"><GitBranch size={14} /> {t("audit.activeContract")}</span>
        <strong>{activePlan?.planRevisionId ?? t("audit.noActiveContract")}</strong>
        <code title={activePlan?.activationDigest}>{shortDigest(activePlan?.activationDigest)}</code>
      </div>
      <div className="inspector-block">
        <span className="inspector-label">Goal revision</span>
        <strong>{snapshot.goal.revisionId}</strong>
        <code title={snapshot.goal.definitionDigest}>{shortDigest(snapshot.goal.definitionDigest)}</code>
      </div>
      <div className="inspector-block">
        <span className="inspector-label">{t("audit.actor")}</span>
        <strong>{recent?.actorRef ?? "—"}</strong>
        <small>{recent?.source ?? "—"} · event #{recent?.sequence ?? 0}</small>
      </div>
      <div className="inspector-block">
        <span className="inspector-label">{t("audit.platform")}</span>
        <strong>{platform.shell} · {platform.os}</strong>
        <small>runtime {platform.runtimeAvailable ? "available" : "mock only"}</small>
      </div>
      <div className="inspector-block history-chain">
        <span className="inspector-label">최근 원장 event</span>
        {snapshot.history.slice(-3).reverse().map((event) => (
          <div className="mini-event" key={event.sequence}>
            <span>#{event.sequence} · {event.eventType}</span>
            <small>{event.summary}</small>
          </div>
        ))}
      </div>
      <div className="prototype-notice">
        <strong>{t("app.nonAuthority")}</strong>
        <p>{t("app.nonAuthorityDescription")}</p>
      </div>
    </aside>
  );
}

export function EmptyState({ title, description, compact = false, children }: { title: string; description: string; compact?: boolean; children?: ReactNode }) {
  return (
    <div className={`empty-state ${compact ? "empty-state--compact" : ""}`}>
      <ShieldCheck size={compact ? 24 : 32} />
      <h2>{title}</h2>
      <p>{description}</p>
      {children}
    </div>
  );
}

interface ModalProps {
  title: string;
  description?: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ title, description, open, onClose, children }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKey);
    requestAnimationFrame(() => dialogRef.current?.querySelector<HTMLElement>("button, input, textarea, select")?.focus());
    return () => {
      document.removeEventListener("keydown", handleKey);
      previous?.focus();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" aria-describedby={description ? "modal-description" : undefined} ref={dialogRef}>
        <div className="modal-heading">
          <div><p className="eyebrow">PLAN ACTIVATION</p><h2 id="modal-title">{title}</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label={t("common.close")}><X size={19} /></button>
        </div>
        {description ? <p id="modal-description" className="modal-description">{description}</p> : null}
        {children}
      </div>
    </div>
  );
}
