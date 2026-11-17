import type {
  ActivatePlanCommand,
  CommandContext,
  CommandResult,
  EngineClient,
  FinalReportPayload,
  ScenarioKey,
  WorkspaceSnapshot,
} from "./contracts";

export interface HttpEngineClientOptions {
  baseUrl?: string;
  sessionToken?: string;
  fetchImpl?: typeof fetch;
  eventSourceFactory?: (url: string) => EventSource;
}

function resolveBootstrappedSessionToken(explicitToken?: string): string | null {
  if (typeof explicitToken === "string" && explicitToken.trim().length > 0) {
    return explicitToken.trim();
  }
  const envObj =
    typeof import.meta !== "undefined"
      ? (import.meta as { env?: Record<string, string | undefined> }).env
      : undefined;
  const envToken = envObj?.VITE_FLOWMARSHAL_SESSION_TOKEN;
  if (typeof envToken === "string" && envToken.trim().length > 0) {
    return envToken.trim();
  }
  if (typeof window !== "undefined") {
    const winToken = (window as unknown as { __FLOWMARSHAL_SESSION_TOKEN__?: unknown })
      .__FLOWMARSHAL_SESSION_TOKEN__;
    if (typeof winToken === "string" && winToken.trim().length > 0) {
      return winToken.trim();
    }
    const meta = window.document?.querySelector('meta[name="flowmarshal-session-token"]');
    const metaContent = meta?.getAttribute("content");
    if (typeof metaContent === "string" && metaContent.trim().length > 0) {
      return metaContent.trim();
    }
  }
  return null;
}

export class HttpEngineClient implements EngineClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly eventSourceFactory?: (url: string) => EventSource;
  private readonly explicitSessionToken?: string;
  private sessionToken: string | null;
  private readonly listeners = new Set<(snapshot: WorkspaceSnapshot) => void>();
  private latestSnapshot: WorkspaceSnapshot | null = null;
  private eventSource: EventSource | null = null;
  private subscribeGeneration = 0;
  private connectingGeneration: number | null = null;

  constructor(options: HttpEngineClientOptions = {}) {
    const envBase =
      typeof import.meta !== "undefined" &&
      typeof (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_ENGINE_API_URL === "string"
        ? (import.meta as { env: Record<string, string> }).env.VITE_ENGINE_API_URL
        : "";
    this.baseUrl = (options.baseUrl ?? envBase).replace(/\/+$/, "");
    this.explicitSessionToken = options.sessionToken;
    this.sessionToken = resolveBootstrappedSessionToken(options.sessionToken);
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
    this.eventSourceFactory =
      options.eventSourceFactory ??
      (typeof EventSource !== "undefined" ? (url: string) => new EventSource(url) : undefined);
  }

  private url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  private notify(snapshot: WorkspaceSnapshot): WorkspaceSnapshot {
    this.latestSnapshot = snapshot;
    for (const listener of this.listeners) {
      listener(snapshot);
    }
    return snapshot;
  }

  private async ensureSessionToken(): Promise<string | null> {
    if (!this.sessionToken) {
      this.sessionToken = resolveBootstrappedSessionToken(this.explicitSessionToken);
    }
    return this.sessionToken;
  }

  private async requestJson<T>(path: string, init?: RequestInit, retriedToken = false): Promise<T> {
    const tokenHeaders: Record<string, string> = {};
    if (path !== "/api/v1/health") {
      const token = await this.ensureSessionToken();
      if (token) {
        tokenHeaders["X-FlowMarshal-Token"] = token;
      }
    }
    const response = await this.fetchImpl(this.url(path), {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...tokenHeaders,
        ...(init?.headers ?? {}),
      },
    });
    if (response.status === 403 && path !== "/api/v1/health" && !retriedToken) {
      const prevToken = this.sessionToken;
      this.sessionToken = resolveBootstrappedSessionToken(this.explicitSessionToken);
      if (this.sessionToken && this.sessionToken !== prevToken) {
        return this.requestJson<T>(path, init, true);
      }
    }
    if (!response.ok) {
      let detail = "";
      try {
        const errBody = (await response.json()) as {
          error?: { code?: string; message?: string } | string;
          message?: string;
        };
        if (errBody && typeof errBody.error === "object" && errBody.error !== null) {
          const code = errBody.error.code ? `[${errBody.error.code}] ` : "";
          detail = `${code}${errBody.error.message ?? ""}`.trim();
        } else if (typeof errBody?.error === "string") {
          detail = errBody.error;
        } else if (typeof errBody?.message === "string") {
          detail = errBody.message;
        }
      } catch {
        // Ignore body parse errors and fallback to status code
      }
      throw new Error(
        detail
          ? `Engine HTTP error (${response.status}): ${detail}`
          : `Engine HTTP error (${response.status}): ${path}`,
      );
    }
    return (await response.json()) as T;
  }

  private handleCommandResult(result: CommandResult): CommandResult {
    this.notify(result.data);
    return result;
  }

  async getSnapshot(): Promise<WorkspaceSnapshot> {
    const snapshot = await this.requestJson<WorkspaceSnapshot>("/api/v1/snapshot");
    return this.notify(snapshot);
  }

  subscribe(listener: (snapshot: WorkspaceSnapshot) => void): () => void {
    this.listeners.add(listener);
    if (this.latestSnapshot) {
      listener(this.latestSnapshot);
    }
    if (this.eventSourceFactory && !this.eventSource && this.connectingGeneration === null) {
      const myGeneration = ++this.subscribeGeneration;
      this.connectingGeneration = myGeneration;
      void (async () => {
        try {
          const token = await this.ensureSessionToken();
          if (
            this.connectingGeneration !== myGeneration ||
            this.listeners.size === 0 ||
            this.eventSource
          ) {
            return;
          }
          const query = token
            ? `/api/v1/events?stream=1&token=${encodeURIComponent(token)}`
            : "/api/v1/events?stream=1";
          const es = this.eventSourceFactory!(this.url(query));
          if (
            this.connectingGeneration !== myGeneration ||
            this.listeners.size === 0 ||
            this.eventSource
          ) {
            es.close();
            return;
          }
          es.addEventListener("snapshot", (event: MessageEvent) => {
            try {
              const parsed = JSON.parse(String(event.data)) as WorkspaceSnapshot;
              if (parsed && typeof parsed === "object" && "phase" in parsed) {
                this.notify(parsed);
              }
            } catch {
              // Ignore malformed SSE payload
            }
          });
          if (typeof es.addEventListener === "function") {
            es.addEventListener("message", (event: MessageEvent) => {
              try {
                const parsed = JSON.parse(String(event.data)) as {
                  event_type?: string;
                  sequence?: number;
                  phase?: string;
                };
                if (parsed && "phase" in parsed) {
                  this.notify(parsed as unknown as WorkspaceSnapshot);
                } else if (
                  parsed?.event_type === "snapshot.ready" &&
                  (!this.latestSnapshot || parsed.sequence !== this.latestSnapshot.historyCursor.sequence)
                ) {
                  void this.getSnapshot().catch(() => undefined);
                }
              } catch {
                // Ignore malformed default message
              }
            });
          }
          this.eventSource = es;
        } catch {
          if (this.connectingGeneration === myGeneration) {
            this.eventSource = null;
          }
        } finally {
          if (this.connectingGeneration === myGeneration) {
            this.connectingGeneration = null;
          }
        }
      })();
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) {
        this.subscribeGeneration += 1;
        this.connectingGeneration = null;
        if (this.eventSource) {
          this.eventSource.close();
          this.eventSource = null;
        }
      }
    };
  }

  async resetScenario(scenario: ScenarioKey): Promise<WorkspaceSnapshot> {
    const snapshot = await this.requestJson<WorkspaceSnapshot>("/api/v1/scenario", {
      method: "POST",
      body: JSON.stringify({ scenario }),
    });
    return this.notify(snapshot);
  }

  async startGoalRevision(context: CommandContext): Promise<WorkspaceSnapshot> {
    const snapshot = await this.requestJson<WorkspaceSnapshot>("/api/v1/goals/draft", {
      method: "POST",
      body: JSON.stringify({ context }),
    });
    return this.notify(snapshot);
  }

  async prepareGoal(sourceRequest: string, context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/goals/prepare", {
      method: "POST",
      body: JSON.stringify({ sourceRequest, context }),
    });
    return this.handleCommandResult(result);
  }

  async searchPlans(context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/plans/search", {
      method: "POST",
      body: JSON.stringify({ context }),
    });
    return this.handleCommandResult(result);
  }

  async activatePlan(command: ActivatePlanCommand, context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/plans/activate", {
      method: "POST",
      body: JSON.stringify({ ...command, context }),
    });
    return this.handleCommandResult(result);
  }

  async runOnce(context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/run-once", {
      method: "POST",
      body: JSON.stringify({ context }),
    });
    return this.handleCommandResult(result);
  }

  async refreshStale(context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/stale/refresh", {
      method: "POST",
      body: JSON.stringify({ context }),
    });
    return this.handleCommandResult(result);
  }

  async observeAttempt(context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/attempts/observe", {
      method: "POST",
      body: JSON.stringify({ context }),
    });
    return this.handleCommandResult(result);
  }

  async resumeAttempt(context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/attempts/resume", {
      method: "POST",
      body: JSON.stringify({ context }),
    });
    return this.handleCommandResult(result);
  }

  async abandonIntent(rationale: string, context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/intents/abandon", {
      method: "POST",
      body: JSON.stringify({ rationale, context }),
    });
    return this.handleCommandResult(result);
  }

  async pauseWorkflow(reason: string, context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/pause", {
      method: "POST",
      body: JSON.stringify({ reason, context }),
    });
    return this.handleCommandResult(result);
  }

  async cancelWorkflow(reason: string, context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/cancel", {
      method: "POST",
      body: JSON.stringify({ reason, context }),
    });
    return this.handleCommandResult(result);
  }

  async replanWorkflow(rationale: string, context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/replan", {
      method: "POST",
      body: JSON.stringify({ rationale, context }),
    });
    return this.handleCommandResult(result);
  }

  async selectProject(projectRoot: string, context: CommandContext): Promise<CommandResult> {
    const result = await this.requestJson<CommandResult>("/api/v1/projects/select", {
      method: "POST",
      body: JSON.stringify({ projectRoot, context }),
    });
    return this.handleCommandResult(result);
  }

  async getFinalReport(): Promise<CommandResult<FinalReportPayload>> {
    const result = await this.requestJson<CommandResult<FinalReportPayload>>("/api/v1/final-report");
    if (result.ok && result.data.snapshot) {
      this.notify(result.data.snapshot);
    }
    return result;
  }
}
