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

export class HttpEngineClient implements EngineClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly eventSourceFactory?: (url: string) => EventSource;
  private sessionToken: string | null;
  private tokenPromise: Promise<string | null> | null = null;
  private readonly listeners = new Set<(snapshot: WorkspaceSnapshot) => void>();
  private latestSnapshot: WorkspaceSnapshot | null = null;
  private eventSource: EventSource | null = null;

  constructor(options: HttpEngineClientOptions = {}) {
    const envBase =
      typeof import.meta !== "undefined" &&
      typeof (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_ENGINE_API_URL === "string"
        ? (import.meta as { env: Record<string, string> }).env.VITE_ENGINE_API_URL
        : "";
    this.baseUrl = (options.baseUrl ?? envBase).replace(/\/+$/, "");
    this.sessionToken = options.sessionToken ?? null;
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
    if (this.sessionToken) {
      return this.sessionToken;
    }
    if (!this.tokenPromise) {
      this.tokenPromise = (async () => {
        try {
          const response = await this.fetchImpl(this.url("/api/v1/health"), {
            method: "GET",
            headers: { "Content-Type": "application/json" },
          });
          if (response.ok) {
            const payload = (await response.json()) as { session_token?: string };
            if (typeof payload.session_token === "string" && payload.session_token.length > 0) {
              this.sessionToken = payload.session_token;
              return this.sessionToken;
            }
          }
        } catch {
          // Ignore and let subsequent POST surface the HTTP error if unreachable
        }
        return null;
      })();
    }
    const token = await this.tokenPromise;
    if (!token) {
      this.tokenPromise = null;
    }
    return token;
  }

  private async requestJson<T>(path: string, init?: RequestInit, retriedToken = false): Promise<T> {
    const method = (init?.method ?? "GET").toUpperCase();
    const tokenHeaders: Record<string, string> = {};
    if (method === "POST") {
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
    if (response.status === 403 && method === "POST" && !retriedToken) {
      this.sessionToken = null;
      this.tokenPromise = null;
      return this.requestJson<T>(path, init, true);
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
    if (this.listeners.size === 1 && this.eventSourceFactory && !this.eventSource) {
      try {
        const es = this.eventSourceFactory(this.url("/api/v1/events?stream=1"));
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
        this.eventSource = null;
      }
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0 && this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
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
