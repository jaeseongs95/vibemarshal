import type { CommandContext, CommandResult, EngineClient } from "./contracts";

interface WebMcpTool {
  name: string;
  title?: string;
  description: string;
  inputSchema: object;
  annotations?: { readOnlyHint?: boolean; untrustedContentHint?: boolean };
  execute(input: unknown): unknown | Promise<unknown>;
}

interface ModelContext {
  registerTool(tool: WebMcpTool, options?: { signal?: AbortSignal }): void | Promise<void>;
}

interface RegisterOptions {
  engine: EngineClient;
  createContext: () => CommandContext;
  onResult: (result: CommandResult) => void;
  documentRef?: Document & { modelContext?: ModelContext };
  reportError?: (error: unknown) => void;
}

export function registerRunOnceWebMcpTool({ engine, createContext, onResult, documentRef, reportError = console.error }: RegisterOptions): () => void {
  const targetDocument = documentRef ?? (typeof document === "undefined" ? undefined : document as Document & { modelContext?: ModelContext });
  const modelContext = targetDocument?.modelContext;
  if (!modelContext?.registerTool) return () => undefined;

  const lifecycle = new AbortController();
  const tool: WebMcpTool = {
    name: "run_vibemarshal_next_step",
    title: "VibeMarshal 다음 한 단계 실행",
    description: "현재 활성 Plan에서 Core가 허용한 run once 한 단계만 수행하고 화면 snapshot을 갱신합니다.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      if (typeof input !== "object" || input === null || Array.isArray(input) || Object.keys(input as object).length > 0) {
        throw new TypeError("입력은 빈 객체여야 합니다.");
      }
      const result = await engine.runOnce(createContext());
      onResult(result);
      if (!result.ok) {
        return { ok: false, code: result.error.code, message: result.error.message, historySequence: result.error.historyCursor.sequence };
      }
      return {
        ok: true,
        phase: result.data.phase,
        historySequence: result.data.historyCursor.sequence,
        activeTask: result.data.tasks.find((task) => task.status !== "completed")?.taskRef ?? null,
        verdict: result.data.validation.verdict,
      };
    },
  };

  try {
    void Promise.resolve(modelContext.registerTool(tool, { signal: lifecycle.signal })).catch(reportError);
  } catch (error) {
    reportError(error);
  }
  return () => lifecycle.abort();
}
