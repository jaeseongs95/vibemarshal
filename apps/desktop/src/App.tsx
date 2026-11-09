import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, X } from "lucide-react";

import { AuditPanel, Header, ProjectRail, SectionTabs, type RuntimeMode } from "./components";
import { createCommandContextFactory } from "./command-context";
import type {
  ActivatePlanCommand,
  AppView,
  CommandResult,
  EngineClient,
  PossibleActionKind,
  ScenarioKey,
  WorkspaceSnapshot,
} from "./contracts";
import { HttpEngineClient } from "./http-engine-client";
import { MockEngineClient } from "./mock-engine-client";
import { BrowserPlatformAdapter } from "./platform";
import { GoalScreen, OverviewScreen, PlansScreen, RecoveryScreen, RunScreen } from "./screens";
import { registerRunOnceWebMcpTool } from "./webmcp";

const validViews: AppView[] = ["overview", "goal", "plans", "run", "recovery"];

function viewFromHash(): AppView {
  if (typeof window === "undefined") return "overview";
  const candidate = window.location.hash.replace(/^#\/?/, "") as AppView;
  return validViews.includes(candidate) ? candidate : "overview";
}

function initialRuntimeMode(explicitMode?: RuntimeMode): RuntimeMode {
  if (explicitMode) return explicitMode;
  const envObj =
    typeof import.meta !== "undefined"
      ? (import.meta as { env?: Record<string, string | boolean | undefined> }).env
      : undefined;
  const envMode = envObj?.VITE_ENGINE_RUNTIME;
  if (envMode === "mock" || envMode === "http") return envMode;
  if (typeof window !== "undefined") {
    const params = new URLSearchParams(window.location.search);
    if (params.get("runtime") === "http") return "http";
    if (params.get("runtime") === "mock") return "mock";
    const isTestEnv = Boolean(envObj?.VITEST || envObj?.MODE === "test");
    if (!isTestEnv && window.location.port && window.location.port !== "5173") {
      return "http";
    }
  }
  return "mock";
}

export interface AppProps {
  client?: EngineClient;
  defaultRuntimeMode?: RuntimeMode;
  apiBaseUrl?: string;
}

export function App({ client, defaultRuntimeMode, apiBaseUrl }: AppProps = {}) {
  const [runtimeMode, setRuntimeMode] = useState<RuntimeMode>(() => initialRuntimeMode(defaultRuntimeMode));
  const mockClientRef = useRef<MockEngineClient | null>(null);
  if (!mockClientRef.current) mockClientRef.current = new MockEngineClient();
  const httpClient = useMemo(() => new HttpEngineClient({ baseUrl: apiBaseUrl }), [apiBaseUrl]);
  const engine: EngineClient = client ?? (runtimeMode === "http" ? httpClient : mockClientRef.current);
  const contextFactoryRef = useRef(createCommandContextFactory());
  const platformRef = useRef(new BrowserPlatformAdapter());
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const [view, setView] = useState<AppView>(viewFromHash);
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<{ code: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [projectsOpen, setProjectsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [finalReportMarkdown, setFinalReportMarkdown] = useState<string | null>(null);

  const navigate = useCallback((nextView: AppView) => {
    setView(nextView);
    if (typeof window !== "undefined" && viewFromHash() !== nextView) window.history.pushState(null, "", `#/${nextView}`);
    setProjectsOpen(false);
    setInspectorOpen(false);
  }, []);

  useEffect(() => {
    let active = true;
    const unsubscribe = engine.subscribe((nextSnapshot) => {
      if (active) setSnapshot(nextSnapshot);
    });
    void engine
      .getSnapshot()
      .then((nextSnapshot) => {
        if (active) setSnapshot(nextSnapshot);
      })
      .catch((error: unknown) => {
        if (!active) return;
        setCommandError({
          code: "ENGINE_CONNECTION_FAILED",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    const handleNavigation = () => setView(viewFromHash());
    window.addEventListener("hashchange", handleNavigation);
    window.addEventListener("popstate", handleNavigation);
    return () => {
      active = false;
      unsubscribe();
      window.removeEventListener("hashchange", handleNavigation);
      window.removeEventListener("popstate", handleNavigation);
    };
  }, [engine]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      const index = Number(event.key) - 1;
      if (index >= 0 && index < validViews.length) {
        event.preventDefault();
        navigate(validViews[index]);
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [navigate]);

  useEffect(() => {
    if (!snapshot?.plans.length) {
      setSelectedPlanId(null);
      return;
    }
    if (!snapshot.plans.some((plan) => plan.planRevisionId === selectedPlanId)) setSelectedPlanId(snapshot.plans[0].planRevisionId);
  }, [selectedPlanId, snapshot?.plans]);

  const handleResult = useCallback((result: CommandResult, successView?: AppView) => {
    if (!result.ok) {
      setCommandError({ code: result.error.code, message: result.error.message });
      if (result.error.code === "STALE_EXECUTION_INPUT" || result.error.code === "EXTERNAL_UNKNOWN") navigate("recovery");
      return false;
    }
    setCommandError(null);
    if (successView) navigate(successView);
    return true;
  }, [navigate]);

  const perform = useCallback(async (operation: () => Promise<CommandResult>, successView?: AppView): Promise<boolean> => {
    if (busy) return false;
    setBusy(true);
    try {
      return handleResult(await operation(), successView);
    } catch (error: unknown) {
      setCommandError({
        code: "ENGINE_COMMAND_FAILED",
        message: error instanceof Error ? error.message : String(error),
      });
      return false;
    } finally {
      setBusy(false);
    }
  }, [busy, handleResult]);

  const runUntilSettled = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      for (let step = 0; step < 24; step += 1) {
        const result = await engine.runOnce(contextFactoryRef.current());
        if (!handleResult(result)) break;
        const canContinue = result.data.possibleActions.some(
          (action) => action.kind === "run_once" && action.enabled,
        );
        if (!canContinue || result.data.phase === "completed" || result.data.recovery) {
          break;
        }
      }
    } catch (error: unknown) {
      setCommandError({
        code: "ENGINE_COMMAND_FAILED",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy(false);
    }
  }, [busy, engine, handleResult]);

  useEffect(() => {
    let unregister: () => void = () => undefined;
    const registration = window.setTimeout(() => {
      unregister = registerRunOnceWebMcpTool({
        engine,
        createContext: contextFactoryRef.current,
        onResult: (result) => handleResult(result, result.ok ? "run" : undefined),
      });
    }, 0);
    return () => {
      window.clearTimeout(registration);
      unregister();
    };
  }, [engine, handleResult]);

  const handleScenario = async (scenario: ScenarioKey) => {
    if (busy) return;
    setBusy(true);
    setCommandError(null);
    setFinalReportMarkdown(null);
    try {
      const nextSnapshot = await engine.resetScenario(scenario);
      setSelectedPlanId(nextSnapshot.plans[0]?.planRevisionId ?? null);
      navigate(scenario === "external-unknown" ? "recovery" : "plans");
    } catch (error: unknown) {
      setCommandError({
        code: "ENGINE_COMMAND_FAILED",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy(false);
    }
  };

  const handleOverviewAction = (kind: PossibleActionKind) => {
    switch (kind) {
      case "edit_goal":
        setBusy(true);
        void engine
          .startGoalRevision(contextFactoryRef.current())
          .then(() => {
            setCommandError(null);
            navigate("goal");
          })
          .catch((error: unknown) => {
            setCommandError({
              code: "ENGINE_COMMAND_FAILED",
              message: error instanceof Error ? error.message : String(error),
            });
          })
          .finally(() => {
            setBusy(false);
          });
        break;
      case "prepare_goal":
        navigate("goal");
        break;
      case "search_plans":
        void perform(() => engine.searchPlans(contextFactoryRef.current()), "plans");
        break;
      case "activate_plan":
        navigate("plans");
        break;
      case "run_once":
        void perform(() => engine.runOnce(contextFactoryRef.current()), "run");
        break;
      case "pause":
        if (engine.pauseWorkflow) {
          void perform(() => engine.pauseWorkflow!("user_paused", contextFactoryRef.current()), "run");
        }
        break;
      case "cancel":
        if (engine.cancelWorkflow) {
          void perform(() => engine.cancelWorkflow!("user_cancelled", contextFactoryRef.current()), "overview");
        }
        break;
      case "replan":
        if (engine.replanWorkflow) {
          void perform(() => engine.replanWorkflow!("user_requested_replan", contextFactoryRef.current()), "plans");
        }
        break;
      case "refresh_snapshot":
      case "observe_attempt":
      case "resume_attempt":
      case "abandon_intent":
        navigate("recovery");
        break;
    }
  };

  const startGoalRevision = () => {
    if (busy) return;
    setBusy(true);
    setCommandError(null);
    void engine
      .startGoalRevision(contextFactoryRef.current())
      .catch((error: unknown) => {
        setCommandError({
          code: "ENGINE_COMMAND_FAILED",
          message: error instanceof Error ? error.message : String(error),
        });
      })
      .finally(() => setBusy(false));
  };

  const activatePlan = (command: ActivatePlanCommand): Promise<boolean> => {
    return perform(() => engine.activatePlan(command, contextFactoryRef.current()), "run");
  };

  const handleSelectProject = (projectRoot: string) => {
    if (!engine.selectProject) return;
    setFinalReportMarkdown(null);
    void perform(() => engine.selectProject!(projectRoot, contextFactoryRef.current()), "overview");
  };

  const handleBrowseProject = async () => {
    if (!engine.selectProject || busy) return;
    const selected = await platformRef.current.selectProjectDirectory();
    if (selected?.displayPath) {
      handleSelectProject(selected.displayPath);
    }
  };

  const handleViewFinalReport = async () => {
    if (!engine.getFinalReport || busy) return;
    setBusy(true);
    try {
      const result = await engine.getFinalReport(contextFactoryRef.current());
      if (result.ok) {
        setFinalReportMarkdown(result.data.markdown);
        setCommandError(null);
      } else {
        setCommandError({ code: result.error.code, message: result.error.message });
      }
    } catch (error: unknown) {
      setCommandError({
        code: "ENGINE_COMMAND_FAILED",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy(false);
    }
  };

  const handleRuntimeModeSwitch = useCallback((nextMode: RuntimeMode) => {
    setCommandError(null);
    setFinalReportMarkdown(null);
    setSnapshot(null);
    setRuntimeMode(nextMode);
  }, []);

  if (!snapshot) {
    return (
      <div className="boot-screen">
        <span className="brand-mark" aria-hidden="true">V</span>
        <p>권위 snapshot 경계를 준비하는 중…</p>
        {commandError ? (
          <div className="command-error" role="alert">
            <AlertCircle size={18} />
            <div>
              <strong>{commandError.code}</strong>
              <span>{commandError.message}</span>
            </div>
            {!client && runtimeMode === "http" ? (
              <button
                className="secondary-action"
                type="button"
                onClick={() => handleRuntimeModeSwitch("mock")}
              >
                로컬 Mock 모드로 전환
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="app-frame">
      <Header
        scenario={snapshot.scenario}
        productStatus={snapshot.productStatus}
        runtimeMode={runtimeMode}
        onRuntimeModeChange={client ? undefined : handleRuntimeModeSwitch}
        onScenarioChange={(scenario) => void handleScenario(scenario)}
        onOpenProjects={() => setProjectsOpen(true)}
        onOpenInspector={() => setInspectorOpen(true)}
      />
      {busy ? <div className="command-progress" role="progressbar" aria-label="명령 처리 중" /> : null}
      <div className="workspace">
        <ProjectRail
          snapshot={snapshot}
          open={projectsOpen}
          onClose={() => setProjectsOpen(false)}
          onSelectProject={engine.selectProject ? handleSelectProject : undefined}
          onBrowseProject={engine.selectProject ? () => void handleBrowseProject() : undefined}
        />
        <main className="main-workspace" aria-busy={busy}>
          <SectionTabs view={view} snapshot={snapshot} onNavigate={navigate} />
          {commandError ? (
            <div className="command-error" role="alert">
              <AlertCircle size={18} />
              <div><strong>{commandError.code}</strong><span>{commandError.message}</span></div>
              <button className="icon-button" type="button" onClick={() => setCommandError(null)} aria-label="오류 알림 닫기"><X size={18} /></button>
            </div>
          ) : null}
          {view === "overview" ? <OverviewScreen snapshot={snapshot} onNavigate={navigate} onAction={handleOverviewAction} /> : null}
          {view === "goal" ? (
            <GoalScreen
              snapshot={snapshot}
              onNewRevision={startGoalRevision}
              onPrepare={(source) => void perform(() => engine.prepareGoal(source, contextFactoryRef.current()))}
              onSearchPlans={() => void perform(() => engine.searchPlans(contextFactoryRef.current()), "plans")}
            />
          ) : null}
          {view === "plans" ? <PlansScreen snapshot={snapshot} selectedPlanId={selectedPlanId} onSelect={setSelectedPlanId} onActivate={activatePlan} onNavigate={navigate} /> : null}
          {view === "run" ? (
            <RunScreen
              snapshot={snapshot}
              onRunOnce={() => void perform(() => engine.runOnce(contextFactoryRef.current()))}
              onRunAll={() => void runUntilSettled()}
              onPause={engine.pauseWorkflow ? () => void perform(() => engine.pauseWorkflow!("user_paused", contextFactoryRef.current())) : undefined}
              onCancel={engine.cancelWorkflow ? () => void perform(() => engine.cancelWorkflow!("user_cancelled", contextFactoryRef.current()), "overview") : undefined}
              onReplan={engine.replanWorkflow ? () => void perform(() => engine.replanWorkflow!("user_requested_replan", contextFactoryRef.current()), "plans") : undefined}
              onViewFinalReport={engine.getFinalReport ? () => void handleViewFinalReport() : undefined}
              finalReportMarkdown={finalReportMarkdown}
              onNavigate={navigate}
            />
          ) : null}
          {view === "recovery" ? (
            <RecoveryScreen
              snapshot={snapshot}
              onRefresh={() => void perform(() => engine.refreshStale(contextFactoryRef.current()), "plans")}
              onObserve={() => void perform(() => engine.observeAttempt(contextFactoryRef.current()))}
              onResume={() => void perform(() => engine.resumeAttempt(contextFactoryRef.current()), "run")}
              onAbandon={(rationale) => void perform(() => engine.abandonIntent(rationale, contextFactoryRef.current()), "goal")}
              onNavigate={navigate}
            />
          ) : null}
        </main>
        <AuditPanel
          snapshot={snapshot}
          platform={platformRef.current.getCapabilities()}
          runtimeMode={runtimeMode}
          open={inspectorOpen}
          onClose={() => setInspectorOpen(false)}
        />
      </div>
      {(projectsOpen || inspectorOpen) ? <button className="drawer-scrim" type="button" aria-label="열린 패널 닫기" onClick={() => { setProjectsOpen(false); setInspectorOpen(false); }} /> : null}
    </div>
  );
}
