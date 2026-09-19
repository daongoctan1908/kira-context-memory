import { expect, test } from "@playwright/test";

test("login route is directly addressable", async ({ page }) => {
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Truy cập trợ lý nội bộ" })).toBeVisible();
});

test("new chat and conversation routes render through the SPA fallback", async ({ page }) => {
  await page.goto("/chat/new");
  await expect(page.getByRole("heading", { name: "Bạn muốn hỏi gì?" })).toBeVisible();

  await page.goto("/chat/company-session-01");
  await expect(page.getByRole("heading", { name: "Cuộc trò chuyện" })).toBeVisible();
  await expect(page.getByText("company-session-01")).toBeVisible();
});
