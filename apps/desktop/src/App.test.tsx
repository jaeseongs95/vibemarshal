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

  it("HttpEngineClient가 /api/v1/health의 session_token을 조회해 POST X-FlowMarshal-Token 헤더로 전송한다", async () => {
    const mock = new MockEngineClient("golden-path");
    const snapshot = await mock.getSnapshot();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/health")) {
        return new Response(JSON.stringify({ session_token: "tok_test_session_123" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/v1/run-once")) {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers["X-FlowMarshal-Token"]).toBe("tok_test_session_123");
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
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    const res = await client.runOnce({
      actorRef: "local:test-user",
      clientInstanceId: "vitest:http-client",
      requestId: "req_1",
      idempotencyKey: "idem_1",
    });
    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
