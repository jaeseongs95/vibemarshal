import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { HttpEngineClient } from "./http-engine-client";
import { MockEngineClient } from "./mock-engine-client";

describe("VibeMarshal GUI prototype", () => {
  it("비권위 상태와 Core가 제안한 다음 행동을 표시한다", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "지금 멈춘 이유와 다음 행동을 한눈에" })).toBeVisible();
    expect(screen.getByText("비권위 데모")).toBeVisible();
    expect(screen.getByRole("button", { name: "Plan 비교하기" })).toBeEnabled();
  });

  it("전체 digest를 확인한 뒤 Plan을 활성화한다", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Plan 비교하기" }));
    expect(await screen.findByRole("heading", { name: "실행 의미를 비교한 뒤 하나만 활성화합니다" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "선택 Plan 활성화" }));
    const dialog = screen.getByRole("dialog", { name: "정확한 Plan 계약을 활성화합니다" });
    expect(dialog).toHaveTextContent(`sha256:${"74".repeat(32)}`);
    fireEvent.click(screen.getByRole("button", { name: "이 계약 활성화" }));
    expect(await screen.findByRole("heading", { name: "권위 원장을 한 단계씩 전진시킵니다" })).toBeVisible();
    expect(screen.getByText("T1 · 현재 실패 재현")).toBeVisible();
  });

  it("stale 시나리오에서 활성화를 거부하고 복구 화면으로 이동한다", async () => {
    render(<App />);
    const scenario = await screen.findByLabelText("데모 시나리오");
    fireEvent.change(scenario, { target: { value: "stale-activation" } });
    await waitFor(() => expect(screen.getByRole("heading", { name: "실행 의미를 비교한 뒤 하나만 활성화합니다" })).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: "선택 Plan 활성화" }));
    fireEvent.click(screen.getByRole("button", { name: "이 계약 활성화" }));
    expect((await screen.findAllByText("STALE_EXECUTION_INPUT")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("heading", { name: "확인 중 Project Map이 바뀌었습니다" })).toBeVisible();
    expect(screen.getByRole("button", { name: /최신 상태 불러오기/ })).toBeEnabled();
  });

  it("external_unknown에서 재실행 없이 먼저 관측한다", async () => {
    render(<App />);
    fireEvent.change(await screen.findByLabelText("데모 시나리오"), { target: { value: "external-unknown" } });
    expect(await screen.findByText("EXTERNAL_UNKNOWN")).toBeVisible();
    expect(screen.queryByRole("button", { name: /재실행/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /먼저 관측/ }));
    expect(await screen.findByRole("button", { name: /마지막 checkpoint에서 재개/ })).toBeEnabled();
  });

  it("Goal 화면에서 효과 정책을 표시하고 Run 화면에서 완료까지 연속 실행을 지원한다", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /^목표/ }));
    expect(await screen.findByText(/변경 정책: minimal_change/)).toBeVisible();
    expect(screen.getByText(/금지: 배포/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /^계획/ }));
    fireEvent.click(await screen.findByRole("button", { name: "선택 Plan 활성화" }));
    fireEvent.click(screen.getByRole("button", { name: "이 계약 활성화" }));

    const runAllBtn = await screen.findByRole("button", { name: /완료까지 연속 실행/ });
    fireEvent.click(runAllBtn);
    await waitFor(() => expect(screen.getAllByText("충족").length).toBeGreaterThanOrEqual(1));
  });

  it("HttpEngineClient가 부트스트랩된 sessionToken으로 GET/POST X-FlowMarshal-Token 헤더를 전송하고 /api/v1/health에 의존하지 않는다", async () => {
    const mock = new MockEngineClient("golden-path");
    const snapshot = await mock.getSnapshot();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/health")) {
        throw new Error("/api/v1/health must not be called for session token bootstrap");
      }
      if (url.endsWith("/api/v1/snapshot")) {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers["X-FlowMarshal-Token"]).toBe("tok_bootstrapped_123");
        return new Response(JSON.stringify(snapshot), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/v1/run-once")) {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers["X-FlowMarshal-Token"]).toBe("tok_bootstrapped_123");
        return new Response(
          JSON.stringify({
            ok: true,
            status: "applied",
            commandId: "cmd_1",
            message: "ok",
            historyCursor: snapshot.historyCursor,
            data: snapshot,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected URL: ${url}`);
    });

    const client = new HttpEngineClient({
      baseUrl: "http://127.0.0.1:8765",
      sessionToken: "tok_bootstrapped_123",
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    const fetchedSnap = await client.getSnapshot();
    expect(fetchedSnap.phase).toBe(snapshot.phase);
    const res = await client.runOnce({
      actorRef: "local:test-user",
      clientInstanceId: "vitest:http-client",
      requestId: "req_1",
      idempotencyKey: "idem_1",
    });
    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("HttpEngineClient가 403 응답 시 window.__FLOWMARSHAL_SESSION_TOKEN__이 갱신되었으면 1회 재시도하고 subscribe-해제-재구독 시 EventSource 누수를 막는다", async () => {
    const mock = new MockEngineClient("golden-path");
    const snapshot = await mock.getSnapshot();
    const win = window as unknown as { __FLOWMARSHAL_SESSION_TOKEN__?: string };
    win.__FLOWMARSHAL_SESSION_TOKEN__ = "tok_stale";
    let runOnceCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/run-once")) {
        runOnceCount += 1;
        const headers = (init?.headers ?? {}) as Record<string, string>;
        if (runOnceCount === 1) {
          expect(headers["X-FlowMarshal-Token"]).toBe("tok_stale");
          win.__FLOWMARSHAL_SESSION_TOKEN__ = "tok_fresh";
          return new Response(
            JSON.stringify({
              ok: false,
              status: "rejected",
              commandId: "cmd_stale",
              message: "expired",
              error: { code: "SESSION_TOKEN_REQUIRED", message: "expired", recoverable: true },
            }),
            { status: 403, headers: { "Content-Type": "application/json" } },
          );
        }
        expect(headers["X-FlowMarshal-Token"]).toBe("tok_fresh");
        return new Response(
          JSON.stringify({
            ok: true,
            status: "applied",
            commandId: "cmd_fresh",
            message: "ok",
            historyCursor: snapshot.historyCursor,
            data: snapshot,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected URL: ${url}`);
    });

    const createdSources: Array<{ url: string; closed: boolean; close: () => void; addEventListener: () => void }> = [];
    const client = new HttpEngineClient({
      baseUrl: "http://127.0.0.1:8765",
      fetchImpl: fetchMock as unknown as typeof fetch,
      eventSourceFactory: (url: string) => {
        const fakeEs = {
          url,
          closed: false,
          close() {
            this.closed = true;
          },
          addEventListener() {
            // no-op
          },
        };
        createdSources.push(fakeEs);
        return fakeEs as unknown as EventSource;
      },
    });
    try {
      const res = await client.runOnce({
        actorRef: "local:test-user",
        clientInstanceId: "vitest:http-client",
        requestId: "req_retry",
        idempotencyKey: "idem_retry",
      });
      expect(res.ok).toBe(true);
      expect(runOnceCount).toBe(2);

      // 구독 -> 즉시 해제 -> 재구독 경합 시 이전 비동기 연결 시도는 무효화되어 EventSource가 단 1개만 열려야 함
      const unsub1 = client.subscribe(() => undefined);
      unsub1();
      const unsub2 = client.subscribe(() => undefined);
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(createdSources.length).toBe(1);
      expect(createdSources[0].closed).toBe(false);
      unsub2();
      expect(createdSources[0].closed).toBe(true);
    } finally {
      delete win.__FLOWMARSHAL_SESSION_TOKEN__;
    }
  });

  it("MockEngineClient는 engineSchemaVersion 4를 반환하고 TaskDag는 곡선 path와 다중 행/열로 간선을 렌더링한다", async () => {
    const mock = new MockEngineClient("golden-path");
    const snap = await mock.getSnapshot();
    expect(snap.engineSchemaVersion).toBe(4);

    const { container } = render(<App client={mock} />);
    fireEvent.click(await screen.findByRole("button", { name: "Plan 비교하기" }));
    const paths = container.querySelectorAll("svg path.dag-line");
    expect(paths.length).toBeGreaterThan(0);
  });

  it("Run 화면에서 일시정지·최종 보고서 조회 및 프로젝트 경로 전환이 동작한다", async () => {
    const mock = new MockEngineClient("golden-path");
    render(<App client={mock} />);

    const pathInput = await screen.findByLabelText("워크스페이스 경로 전환");
    fireEvent.change(pathInput, { target: { value: "D:\\gemini\\new-workspace" } });
    fireEvent.submit(pathInput.closest("form")!);
    await waitFor(() => expect(screen.getByText("new-workspace")).toBeVisible());

    fireEvent.click(screen.getByRole("button", { name: /^계획/ }));
    fireEvent.click(await screen.findByRole("button", { name: "선택 Plan 활성화" }));
    fireEvent.click(screen.getByRole("button", { name: "이 계약 활성화" }));

    const pauseBtn = await screen.findByRole("button", { name: "일시정지" });
    fireEvent.click(pauseBtn);

    expect(screen.getByRole("button", { name: "재계획" })).toBeVisible();

    const runAllBtn = await screen.findByRole("button", { name: /완료까지 연속 실행/ });
    fireEvent.click(runAllBtn);
    await waitFor(() => expect(screen.getAllByText("충족").length).toBeGreaterThanOrEqual(1));

    const reportBtn = await screen.findByRole("button", { name: "최종 보고서 조회" });
    fireEvent.click(reportBtn);
    expect(await screen.findByText(/Governance A2 서명 프로파일 및 보안 한계/)).toBeVisible();

    const scenarioSelect = screen.getByLabelText("데모 시나리오");
    fireEvent.change(scenarioSelect, { target: { value: "stale-activation" } });
    await waitFor(() => expect(screen.queryByText(/Governance A2 서명 프로파일 및 보안 한계/)).toBeNull());
  });
});
