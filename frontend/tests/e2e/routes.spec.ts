import { expect, test, type Page, type Route } from "@playwright/test";

const USER = { user_id: "user-01", username: "alice" };

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockAnonymous(page: Page) {
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(
      route,
      {
        code: "AUTH_SESSION_INVALID",
        message: "Authentication required",
        correlation_id: "corr-e2e",
        retryable: false,
      },
      401,
    );
  });
}

async function mockAuthenticated(page: Page) {
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(route, USER);
  });
}

test("login route is directly addressable", async ({ page }) => {
  await mockAnonymous(page);
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Truy cập trợ lý nội bộ" })).toBeVisible();
});

test("new chat and conversation routes rehydrate the session", async ({ page }) => {
  await mockAuthenticated(page);

  await page.goto("/chat/new");
  await expect(page.getByRole("heading", { name: "Bạn muốn hỏi gì?" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Đổi mật khẩu/ })).toBeVisible();

  await page.goto("/chat/company-session-01");
  await expect(page.getByRole("heading", { name: "Cuộc trò chuyện" })).toBeVisible();
  await expect(page.getByText("company-session-01")).toBeVisible();
});

test("login restores the protected conversation requested before authentication", async ({
  page,
}) => {
  let loginBody: unknown;
  await page.route("**/api/v1/auth/**", async (route) => {
    const request = route.request();
    if (request.url().endsWith("/me")) {
      await fulfillJson(
        route,
        {
          code: "AUTH_SESSION_INVALID",
          message: "Authentication required",
          correlation_id: "corr-login",
          retryable: false,
        },
        401,
      );
      return;
    }
    if (request.url().endsWith("/login") && request.method() === "POST") {
      loginBody = request.postDataJSON();
      await fulfillJson(route, USER);
      return;
    }
    await route.abort();
  });

  await page.goto("/chat/company-session-01");
  await page.getByLabel("Tên đăng nhập").fill("alice");
  await page.getByLabel("Mật khẩu").fill("private-password");
  await page.getByRole("button", { name: "Đăng nhập" }).click();

  await expect(page.getByText("company-session-01")).toBeVisible();
  expect(loginBody).toEqual({ username: "alice", password: "private-password" });
});

test("password change sends CSRF and revokes the current frontend session", async ({
  context,
  page,
}) => {
  await context.addCookies([
    {
      name: "kira_csrf_dev",
      value: "csrf-e2e",
      url: "http://127.0.0.1:4173",
    },
  ]);
  let csrfHeader: string | undefined;
  let changeBody: unknown;
  await page.route("**/api/v1/auth/**", async (route) => {
    const request = route.request();
    if (request.url().endsWith("/me")) {
      await fulfillJson(route, USER);
      return;
    }
    if (request.url().endsWith("/change-password") && request.method() === "POST") {
      csrfHeader = request.headers()["x-csrf-token"];
      changeBody = request.postDataJSON();
      await route.fulfill({ status: 204 });
      return;
    }
    await route.abort();
  });

  await page.goto("/account/password");
  await page.getByLabel("Mật khẩu hiện tại").fill("old-password-123");
  await page.getByLabel("Mật khẩu mới", { exact: true }).fill("new-password-123");
  await page.getByLabel("Nhập lại mật khẩu mới").fill("new-password-123");
  await page.getByRole("button", { name: "Đổi mật khẩu" }).click();

  await expect(page.getByText("Mật khẩu đã được đổi. Hãy đăng nhập lại.")).toBeVisible();
  expect(csrfHeader).toBe("csrf-e2e");
  expect(changeBody).toEqual({
    current_password: "old-password-123",
    new_password: "new-password-123",
  });
});
