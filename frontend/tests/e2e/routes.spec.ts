import { expect, test, type Page, type Route } from "@playwright/test";

const USER = { user_id: "user-01", username: "alice" };
const CONVERSATION = {
  conversation_id: "11111111-1111-4111-8111-111111111111",
  session_id: "company-session-01",
  title: "Hỗ trợ chuyển vùng",
  status: "active",
  created_at: "2026-09-18T08:00:00Z",
  updated_at: "2026-09-18T08:00:00Z",
  last_message_at: "2026-09-18T09:00:00Z",
};

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
  await mockConversationApi(page);
}

async function mockConversationApi(page: Page) {
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    if (request.url().includes("/messages?")) {
      await fulfillJson(route, {
        items: [
          {
            turn_id: "turn-01",
            role: "user",
            content: "Tôi cần hỗ trợ chuyển vùng.",
            timestamp: "2026-09-18T09:00:00Z",
          },
          {
            turn_id: "turn-01",
            role: "assistant",
            content: "Tôi có thể hỗ trợ kiểm tra gói phù hợp.",
            timestamp: "2026-09-18T09:00:01Z",
          },
        ],
        next_before_message_id: null,
      });
      return;
    }
    await fulfillJson(route, { items: [CONVERSATION], next_cursor: null });
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
  await expect(page.getByRole("heading", { level: 1, name: "Hỗ trợ chuyển vùng" })).toBeVisible();
  await expect(page.getByText("Tôi có thể hỗ trợ kiểm tra gói phù hợp.")).toBeVisible();
});

test("login restores the protected conversation requested before authentication", async ({
  page,
}) => {
  let loginBody: unknown;
  await mockConversationApi(page);
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

  await expect(page.getByRole("heading", { level: 1, name: "Hỗ trợ chuyển vùng" })).toBeVisible();
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

test("conversation pagination appends the next sidebar page", async ({ page }) => {
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(route, USER);
  });
  await page.route("**/api/v1/conversations**", async (route) => {
    if (route.request().url().includes("cursor=next-page")) {
      await fulfillJson(route, {
        items: [
          {
            ...CONVERSATION,
            conversation_id: "22222222-2222-4222-8222-222222222222",
            session_id: "company-session-02",
            title: "Gói cước doanh nghiệp",
          },
        ],
        next_cursor: null,
      });
      return;
    }
    await fulfillJson(route, { items: [CONVERSATION], next_cursor: "next-page" });
  });

  await page.goto("/chat/new");
  await page.getByRole("button", { name: "Xem thêm" }).click();

  await expect(page.getByText("Hỗ trợ chuyển vùng")).toBeVisible();
  await expect(page.getByText("Gói cước doanh nghiệp")).toBeVisible();
});

test("pending deletion remains visible and can be retried", async ({ context, page }) => {
  await context.addCookies([
    {
      name: "kira_csrf_dev",
      value: "csrf-delete",
      url: "http://127.0.0.1:4173",
    },
  ]);
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(route, USER);
  });
  let attempts = 0;
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    if (request.method() === "DELETE") {
      attempts += 1;
      if (attempts === 1) {
        await fulfillJson(
          route,
          {
            code: "DELETION_RETRY_REQUIRED",
            message: "Retry deletion",
            correlation_id: "corr-delete",
            retryable: true,
          },
          503,
        );
      } else {
        await route.fulfill({ status: 204 });
      }
      return;
    }
    await fulfillJson(route, {
      items: [{ ...CONVERSATION, status: "deletion_pending" }],
      next_cursor: null,
    });
  });

  await page.goto("/chat/new");
  await page.getByRole("button", { name: "Thử lại" }).click();
  await expect(page.getByText("Chưa thể xóa. Hãy thử lại.")).toBeVisible();
  await page.getByRole("button", { name: "Thử lại" }).click();

  await expect(page.getByText("Hỗ trợ chuyển vùng")).toHaveCount(0);
  expect(attempts).toBe(2);
});
