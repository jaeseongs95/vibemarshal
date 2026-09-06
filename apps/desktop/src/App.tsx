import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, X } from "lucide-react";

import { AuditPanel, Header, ProjectRail, SectionTabs } from "./components";
import { createCommandContextFactory } from "./command-context";
import type {
  ActivatePlanCommand,
  AppView,
  CommandResult,
  PossibleActionKind,
  ScenarioKey,
  WorkspaceSnapshot,
} from "./contracts";
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

export function App() {
  const engineRef = useRef<MockEngineClient | null>(null);
  if (!engineRef.current) engineRef.current = new MockEngineClient();
  const engine = engineRef.current;
  const contextFactoryRef = useRef(createCommandContextFactory());
  const platformRef = useRef(new BrowserPlatformAdapter());
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const [view, setView] = useState<AppView>(viewFromHash);
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<{ code: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [projectsOpen, setProjectsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);

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
    void engine.getSnapshot().then((nextSnapshot) => {
      if (active) setSnapshot(nextSnapshot);
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

  const perform = useCallback(async (operation: () => Promise<CommandResult>, successView?: AppView) => {
    if (busy) return;
    setBusy(true);
    try {
      handleResult(await operation(), successView);
    } finally {
      setBusy(false);
    }
  }, [busy, handleResult]);

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
    try {
      const nextSnapshot = await engine.resetScenario(scenario);
      setSelectedPlanId(nextSnapshot.plans[0]?.planRevisionId ?? null);
      navigate(scenario === "external-unknown" ? "recovery" : "plans");
    } finally {
      setBusy(false);
    }
  };

  const handleOverviewAction = (kind: PossibleActionKind) => {
    switch (kind) {
      case "edit_goal":
        setBusy(true);
        void engine.startGoalRevision(contextFactoryRef.current()).finally(() => {
          setBusy(false);
          navigate("goal");
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
    void engine.startGoalRevision(contextFactoryRef.current()).finally(() => setBusy(false));
  };

  const activatePlan = (command: ActivatePlanCommand) => {
    void perform(() => engine.activatePlan(command, contextFactoryRef.current()), "run");
  };

  if (!snapshot) {
    return <div className="boot-screen"><span className="brand-mark" aria-hidden="true">V</span><p>권위 snapshot 경계를 준비하는 중…</p></div>;
  }

  return (
    <div className="app-frame">
      <Header scenario={snapshot.scenario} productStatus={snapshot.productStatus} onScenarioChange={(scenario) => void handleScenario(scenario)} onOpenProjects={() => setProjectsOpen(true)} onOpenInspector={() => setInspectorOpen(true)} />
      {busy ? <div className="command-progress" role="progressbar" aria-label="명령 처리 중" /> : null}
      <div className="workspace">
        <ProjectRail snapshot={snapshot} open={projectsOpen} onClose={() => setProjectsOpen(false)} />
        <main className="main-workspace" aria-busy={busy}>
          <SectionTabs view={view} onNavigate={navigate} />
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
          {view === "run" ? <RunScreen snapshot={snapshot} onRunOnce={() => void perform(() => engine.runOnce(contextFactoryRef.current()))} onNavigate={navigate} /> : null}
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
        <AuditPanel snapshot={snapshot} platform={platformRef.current.getCapabilities()} open={inspectorOpen} onClose={() => setInspectorOpen(false)} />
      </div>
      {(projectsOpen || inspectorOpen) ? <button className="drawer-scrim" type="button" aria-label="열린 패널 닫기" onClick={() => { setProjectsOpen(false); setInspectorOpen(false); }} /> : null}
    </div>
  );
}
