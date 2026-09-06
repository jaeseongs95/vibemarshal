import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "지금 멈춘 이유와 다음 행동을 한눈에" })).toBeVisible();
});

test("exact digest 확인 뒤 각 run once가 한 단계씩 전진해 Goal을 완료한다", async ({ page }) => {
  await page.getByRole("button", { name: "목표" }).click();
  await page.getByRole("button", { name: "새 요청으로 다시 시작" }).click();
  const goalSource = page.getByLabel("사용자 원문");
  await goalSource.fill("결측 usage를 알 수 없음으로 보존하고 두 보고 경로를 회귀 검증해줘.");
  await page.getByRole("button", { name: /정규화하고 독립 검토/ }).click();
  await expect(page.getByText("finding 없음 · 통과")).toBeVisible();
  await page.getByRole("button", { name: /Plan 후보 만들기/ }).click();
  await expect(page.getByRole("heading", { name: "실행 의미를 비교한 뒤 하나만 활성화합니다" })).toBeVisible();
  await page.getByRole("button", { name: "선택 Plan 활성화" }).click();
  const dialog = page.getByRole("dialog", { name: "정확한 Plan 계약을 활성화합니다" });
  await expect(dialog).toContainText(`sha256:${"74".repeat(32)}`);
  await expect(dialog).toContainText("mock:");
  await dialog.getByRole("button", { name: "이 계약 활성화" }).click();

  await expect(page.getByRole("heading", { name: "권위 원장을 한 단계씩 전진시킵니다" })).toBeVisible();
  await page.getByRole("button", { name: "다음 한 단계 실행" }).click();
  await expect(page.getByText("execution_spec_t1_01")).toBeVisible();
  await expect(page.getByText("명세 고정")).toBeVisible();

  for (let index = 0; index < 13; index += 1) {
    await page.getByRole("button", { name: "다음 한 단계 실행" }).click();
  }
  await expect(page.getByText("충족", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "다음 한 단계 실행" })).toHaveCount(0);
});

test("stale 활성화는 최신 Plan revision 재확인 전까지 차단한다", async ({ page }) => {
  await page.getByLabel("데모 시나리오").selectOption("stale-activation");
  await page.getByRole("button", { name: "선택 Plan 활성화" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "이 계약 활성화" }).click();

  await expect(page.getByRole("heading", { name: "확인 중 Project Map이 바뀌었습니다" })).toBeVisible();
  await expect(page.getByText("provider 호출 0회")).toBeVisible();
  await page.getByRole("button", { name: /최신 상태 불러오기/ }).click();
  await expect(page.getByText("plan_revision_demo_05").first()).toBeVisible();
  await page.getByRole("button", { name: "선택 Plan 활성화" }).click();
  await expect(page.getByRole("dialog")).toContainText(`sha256:${"86".repeat(32)}`);
});

test("external_unknown은 재실행 대신 observe 후 기존 thread를 재개한다", async ({ page }) => {
  await page.getByLabel("데모 시나리오").selectOption("external-unknown");
  await expect(page.getByText("EXTERNAL_UNKNOWN")).toBeVisible();
  await expect(page.getByRole("button", { name: /재실행/ })).toHaveCount(0);
  await page.getByRole("button", { name: /먼저 관측/ }).click();
  await expect(page.getByText("thread/read 완료")).toBeVisible();
  await page.getByRole("button", { name: /마지막 checkpoint에서 재개/ }).click();
  await expect(page.getByRole("heading", { name: "권위 원장을 한 단계씩 전진시킵니다" })).toBeVisible();
  await expect(page.getByText("thread_demo_7f4a")).toBeVisible();
});

test("모바일에서는 프로젝트와 감사 패널을 drawer로 연다", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "모바일 viewport 전용 검증");
  await page.getByRole("button", { name: "프로젝트 목록 열기" }).click();
  const projectDrawer = page.getByRole("complementary", { name: "프로젝트" });
  await expect(projectDrawer).toBeVisible();
  await projectDrawer.getByRole("button", { name: "닫기" }).click();
  await page.getByRole("button", { name: "상태 및 감사 정보 열기" }).click();
  await expect(page.getByRole("complementary", { name: "상태 및 감사 정보" })).toBeVisible();
  await expect(page.getByText("비권위 데모")).toBeVisible();
});
