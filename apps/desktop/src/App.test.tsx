import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

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
});
