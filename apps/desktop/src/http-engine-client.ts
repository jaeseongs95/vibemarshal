import type {
  ActivatePlanCommand,
  CommandContext,
  CommandResult,
  EngineClient,
  ScenarioKey,
  WorkspaceSnapshot,
} from "./contracts";

export interface HttpEngineClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  eventSourceFactory?: (url: string) => EventSource;
}

export class HttpEngineClient implements EngineClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly eventSourceFactory?: (url: string) => EventSource;
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

  private async requestJson<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await this.fetchImpl(this.url(path), {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
    if (!response.ok) {
      throw new Error(`Engine HTTP error (${response.status}): ${path}`);
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
        const es = this.eventSourceFactory(this.url("/api/v1/events"));
        es.addEventListener("snapshot", (event: MessageEvent) => {
          try {
            const parsed = JSON.parse(String(event.data)) as WorkspaceSnapshot;
            this.notify(parsed);
          } catch {
            // Ignore malformed SSE payload
          }
        });
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
}
