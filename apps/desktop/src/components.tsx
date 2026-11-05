import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  Check,
  CircleDot,
  Copy,
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

const phaseLabels: Record<WorkspaceSnapshot["phase"], string> = {
  goal_draft: "목표 작성 중",
  goal_ready: "목표 준비됨",
  plan_ready: "계획 비교 대기",
  plan_active: "계획 활성화됨",
  running: "실행 중",
  recovery: "복구 필요",
  completed: "검증 완료",
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

export function CopyableCode({
  value,
  short = true,
}: {
  value: string | null | undefined;
  short?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  if (!value) return <code>—</code>;
  const display = short ? shortDigest(value) : value;

  const handleCopy = () => {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(value);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  };

  return (
    <span className="copyable-code-wrap">
      <code title={value}>{display}</code>
      <button
        type="button"
        className={`copy-digest-btn ${copied ? "is-copied" : ""}`}
        onClick={handleCopy}
        title={copied ? "복사됨" : "전체 값 복사"}
        aria-label={copied ? "복사됨" : `${value} 복사`}
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
      </button>
    </span>
  );
}

export type RuntimeMode = "mock" | "http";

interface HeaderProps {
  scenario: ScenarioKey;
  productStatus: WorkspaceSnapshot["productStatus"];
  runtimeMode?: RuntimeMode;
  onRuntimeModeChange?: (mode: RuntimeMode) => void;
  onScenarioChange: (scenario: ScenarioKey) => void;
  onOpenProjects: () => void;
  onOpenInspector: () => void;
}

export function Header({
  scenario,
  productStatus,
  runtimeMode = "mock",
  onRuntimeModeChange,
  onScenarioChange,
  onOpenProjects,
  onOpenInspector,
}: HeaderProps) {
  const nextMode: RuntimeMode = runtimeMode === "mock" ? "http" : "mock";
  const runtimeLabel = runtimeMode === "http" ? "Local HTTP Engine" : t("app.runtimeMock");
  return (
    <header className="topbar">
      <div className="topbar-start">
        <button className="icon-button mobile-only" type="button" onClick={onOpenProjects} aria-label={t("nav.openProjects")}>
          <Menu size={19} />
        </button>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">V</span>
          <span>{t("app.brand")}</span>
          <span className="prototype-label">{runtimeMode === "http" ? "v1.0.1 Engine" : t("app.prototype")}</span>
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
        {onRuntimeModeChange ? (
          <button
            className={`status-chip runtime-chip ${runtimeMode === "http" ? "runtime-chip--live" : ""}`}
            type="button"
            onClick={() => onRuntimeModeChange(nextMode)}
            title="클릭하여 Mock / Local HTTP Engine 전환"
          >
            <CircleDot size={14} /> {runtimeLabel}
          </button>
        ) : (
          <span className={`status-chip runtime-chip ${runtimeMode === "http" ? "runtime-chip--live" : ""}`}>
            <CircleDot size={14} /> {runtimeLabel}
          </span>
        )}
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
  const completedCount = snapshot.tasks.filter((task) => task.status === "completed").length;
  const progress = snapshot.tasks.length
    ? `${completedCount}/${snapshot.tasks.length} 완료`
    : "활성 Plan 없음";
  return (
    <aside className={`project-rail ${open ? "is-drawer-open" : ""}`} aria-label="프로젝트">
      <div className="rail-heading">
        <span>{t("app.projects")}</span>
        <button className="icon-button drawer-close" type="button" onClick={onClose} aria-label={t("nav.close")}><X size={18} /></button>
      </div>
      <button className="project-item project-item--active" type="button" onClick={onClose} aria-current="true">
        <span className="project-glyph"><FolderKanban size={18} /></span>
        <span>
          <strong>{snapshot.project.name}</strong>
          <small>{snapshot.project.runState === "recovery_required" ? "복구 필요" : progress}</small>
        </span>
      </button>

      <div className="rail-meta-card" aria-label="워크스페이스 상태 요약">
        <div className="rail-meta-row">
          <span>현재 단계</span>
          <strong>{phaseLabels[snapshot.phase] ?? snapshot.phase}</strong>
        </div>
        <div className="rail-meta-row">
          <span>경로</span>
          <strong title={snapshot.project.location.displayPath}>{snapshot.project.location.displayPath}</strong>
        </div>
        <div className="rail-meta-row">
          <span>Goal Revision</span>
          <strong>#{snapshot.goal.revisionNo} ({statusLabels[snapshot.goal.status] ?? snapshot.goal.status})</strong>
        </div>
        <div className="rail-meta-row">
          <span>원장 이벤트</span>
          <strong>#{snapshot.historyCursor.sequence}</strong>
        </div>
      </div>

      <div className="rail-footnote">
        <LockKeyhole size={15} />
        <span>{t("app.localWriter")}</span>
      </div>
    </aside>
  );
}

interface SectionTabsProps {
  view: AppView;
  snapshot?: WorkspaceSnapshot;
  onNavigate: (view: AppView) => void;
}

export function SectionTabs({ view, snapshot, onNavigate }: SectionTabsProps) {
  const completedTasks = snapshot ? snapshot.tasks.filter((task) => task.status === "completed").length : 0;
  const totalTasks = snapshot ? snapshot.tasks.length : 0;

  const renderBadge = (item: AppView) => {
    if (!snapshot) return null;
    if (item === "plans" && snapshot.plans.length > 0) {
      return <span className="tab-pill">{snapshot.plans.length}</span>;
    }
    if (item === "run" && totalTasks > 0) {
      return (
        <span className={`tab-pill ${completedTasks === totalTasks ? "tab-pill--done" : ""}`}>
          {completedTasks}/{totalTasks}
        </span>
      );
    }
    if (item === "recovery" && snapshot.recovery) {
      return <span className="tab-pill tab-pill--alert" aria-label="복구 필요">!</span>;
    }
    return null;
  };

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
          <span>{viewLabels[item]}</span>
          {renderBadge(item)}
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
              <span>
                {task.executionSpecRevisionId
                  ? `Spec: ${shortDigest(task.executionSpecRevisionId)}`
                  : task.dependencies.length
                    ? `선행 ${task.dependencies.join(", ")}`
                    : "dependency 없음"}
                {task.contributesTo.length ? ` · AC: ${task.contributesTo.join(", ")}` : ""}
              </span>
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
  const count = Math.max(plan.tasks.length, 1);
  const nodeWidth = 156;
  const nodeHeight = 72;
  const minGap = 28;
  const width = Math.max(680, count * nodeWidth + (count + 1) * minGap);
  const top = 24;
  const gap = (width - nodeWidth * count) / Math.max(count + 1, 2);
  const positions = new Map(plan.tasks.map((task, index) => [task.taskRef, { x: gap + index * (nodeWidth + gap), y: top }]));
  return (
    <div className="dag-wrap">
      <svg viewBox={`0 0 ${width} 124`} role="img" aria-labelledby={`dag-title-${plan.planRevisionId} dag-desc-${plan.planRevisionId}`}>
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
          const acText = task.contributesTo.length ? task.contributesTo.join(" · ") : "AC 연결";
          return (
            <g key={task.taskRef}>
              <title>{`${task.taskRef}: ${task.title} (${acText})`}</title>
              <rect x={position.x} y={position.y} width={nodeWidth} height={nodeHeight} rx="10" className="dag-node" />
              <text x={position.x + 12} y={position.y + 22} className="dag-ref">{task.taskRef}</text>
              <text x={position.x + 12} y={position.y + 42} className="dag-title">{task.title.length > 14 ? `${task.title.slice(0, 14)}…` : task.title}</text>
              <text x={position.x + 12} y={position.y + 60} className="dag-ac">{acText.length > 18 ? `${acText.slice(0, 18)}…` : acText}</text>
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
  runtimeMode?: RuntimeMode;
  open: boolean;
  onClose: () => void;
}

export function AuditPanel({ snapshot, platform, runtimeMode = "mock", open, onClose }: AuditPanelProps) {
  const activePlan = snapshot.plans.find((plan) => plan.planRevisionId === snapshot.activePlanRevisionId);
  const recent = snapshot.history.at(-1);
  const isLiveHttp = runtimeMode === "http";
  return (
    <aside className={`audit-panel ${open ? "is-drawer-open" : ""}`} aria-label="상태 및 감사 정보">
      <div className="inspector-title">
        <Activity size={17} /><strong>{t("audit.title")}</strong>
        <button className="icon-button drawer-close" type="button" onClick={onClose} aria-label={t("nav.close")}><X size={18} /></button>
      </div>
      <dl className="audit-list">
        <div><dt>{t("audit.api")}</dt><dd>호환됨 · v{snapshot.apiVersion}</dd></div>
        <div><dt>{t("audit.permission")}</dt><dd className="ok-text">{isLiveHttp ? "danger-full-access / never" : t("audit.permissionValue")}</dd></div>
        <div><dt>{t("audit.model")}</dt><dd>{isLiveHttp ? "Runtime inventory 결속" : t("audit.modelValue")}</dd></div>
        <div><dt>{t("audit.history")}</dt><dd className="ok-text">{t("audit.historyValue")} (#{snapshot.historyCursor.sequence})</dd></div>
      </dl>
      <hr />
      <div className="inspector-block">
        <span className="inspector-label"><GitBranch size={14} /> {t("audit.activeContract")}</span>
        <strong>{activePlan?.planRevisionId ?? t("audit.noActiveContract")}</strong>
        <CopyableCode value={activePlan?.activationDigest} />
      </div>
      <div className="inspector-block">
        <span className="inspector-label">Goal revision</span>
        <strong>{snapshot.goal.revisionId}</strong>
        <CopyableCode value={snapshot.goal.definitionDigest} />
      </div>
      <div className="inspector-block">
        <span className="inspector-label">{t("audit.actor")}</span>
        <strong>{recent?.actorRef ?? "—"}</strong>
        <small>{recent?.source ?? "—"} · event #{recent?.sequence ?? 0}</small>
      </div>
      <div className="inspector-block">
        <span className="inspector-label">{t("audit.platform")}</span>
        <strong>{platform.shell} · {platform.os}</strong>
        <small>runtime {isLiveHttp ? "local HTTP connected" : platform.runtimeAvailable ? "available" : "mock only"}</small>
      </div>
      <div className="inspector-block history-chain">
        <span className="inspector-label">최근 원장 event ({snapshot.history.length}건)</span>
        <div className="history-scroll">
          {snapshot.history.slice(-6).reverse().map((event) => (
            <div className="mini-event" key={event.sequence}>
              <span>#{event.sequence} · {event.eventType}</span>
              <small>{event.summary}</small>
            </div>
          ))}
        </div>
      </div>
      <div className={`prototype-notice ${isLiveHttp ? "prototype-notice--live" : ""}`}>
        <strong>{isLiveHttp ? "SQLite 권위 원장 연결됨" : t("app.nonAuthority")}</strong>
        <p>
          {isLiveHttp
            ? "로컬 HTTP/SSE 어댑터를 통해 실제 EngineService 및 schema 4 SQLite 원장과 동기화됩니다."
            : t("app.nonAuthorityDescription")}
        </p>
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
