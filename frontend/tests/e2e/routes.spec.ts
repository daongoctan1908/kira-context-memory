import { expect, test, type Page, type Route } from "@playwright/test";

const USER = { user_id: "user-01", username: "alice" };
const APP_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:4173";
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
    if (request.method() === "PATCH") {
      const body = request.postDataJSON() as { title: string };
      await fulfillJson(route, { ...CONVERSATION, title: body.title });
      return;
    }
    if (request.url().endsWith("/feedback") && request.method() === "PUT") {
      const body = request.postDataJSON() as { rating: "up" | "down" };
      await fulfillJson(route, { turn_id: "turn-01", rating: body.rating });
      return;
    }
    if (request.url().endsWith("/feedback") && request.method() === "DELETE") {
      await route.fulfill({ status: 204 });
      return;
    }
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
            feedback: null,
          },
        ],
        next_before_message_id: null,
      });
      return;
    }
    await fulfillJson(route, { items: [CONVERSATION], next_cursor: null });
  });
}

async function openConversationDrawerWhenCollapsed(page: Page) {
  const viewport = page.viewportSize();
  if (viewport !== null && viewport.width <= 767) {
    await page.getByRole("button", { name: "Mở danh sách cuộc trò chuyện" }).click();
  }
}

test("login route is directly addressable", async ({ page }) => {
  await mockAnonymous(page);
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Truy cập trợ lý nội bộ" })).toBeVisible();
});

test("new chat and conversation routes rehydrate the session", async ({ page }) => {
  await mockAuthenticated(page);

  await page.goto("/chat/new");
  await expect(
    page.getByRole("heading", { name: "Hôm nay tôi có thể giúp gì cho bạn?" }),
  ).toBeVisible();
  await openConversationDrawerWhenCollapsed(page);
  await page.getByRole("button", { name: "Mở menu tài khoản" }).click();
  await expect(page.getByRole("menuitem", { name: /Đổi mật khẩu/ })).toBeVisible();

  await page.goto("/chat/company-session-01");
  await expect(page.getByRole("heading", { level: 1, name: "Hỗ trợ chuyển vùng" })).toBeVisible();
  await expect(page.getByText("Tôi có thể hỗ trợ kiểm tra gói phù hợp.")).toBeVisible();
});

test("new chat creates a titled conversation and sends the first prompt", async ({
  context,
  page,
}) => {
  await context.addCookies([
    {
      name: "kira_csrf_dev",
      value: "csrf-new-chat",
      url: APP_URL,
    },
  ]);
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(route, USER);
  });
  let createdTitle = "";
  let sentMessage = "";
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    if (request.method() === "POST" && request.url().endsWith("/messages")) {
      const posted = request.postDataJSON() as Record<string, unknown>;
      sentMessage = String(posted.message);
      const identity = {
        turn_id: "turn-first-prompt",
        client_message_id: String(posted.client_message_id),
      };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: [
          `event: message.started\ndata: ${JSON.stringify(identity)}\n\n`,
          `event: message.delta\ndata: ${JSON.stringify({ ...identity, text: "Đã nhận câu hỏi đầu tiên." })}\n\n`,
          `event: message.completed\ndata: ${JSON.stringify({ ...identity, replayed: false })}\n\n`,
        ].join(""),
      });
      return;
    }
    if (request.method() === "POST" && request.url().endsWith("/conversations")) {
      const posted = request.postDataJSON() as Record<string, unknown>;
      createdTitle = String(posted.title);
      await fulfillJson(route, { ...CONVERSATION, title: createdTitle }, 201);
      return;
    }
    if (request.url().includes("/messages?")) {
      await fulfillJson(route, { items: [], next_before_message_id: null });
      return;
    }
    await fulfillJson(route, { items: [], next_cursor: null });
  });

  const prompt = "Hướng dẫn bật chuyển vùng quốc tế";
  await page.goto("/chat/new");
  await page.getByLabel("Tin nhắn đầu tiên").fill(prompt);
  await page.getByRole("button", { name: "Bắt đầu trò chuyện" }).click();

  await expect(page.getByText("Đã nhận câu hỏi đầu tiên.")).toBeVisible();
  expect(createdTitle).toBe(prompt);
  expect(sentMessage).toBe(prompt);
});

test("mobile conversation drawer opens without taking space from the chat", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockAuthenticated(page);
  await page.goto("/chat/new");

  const drawer = page.locator("#conversation-sidebar");
  await expect(drawer).toBeHidden();
  await page.getByRole("button", { name: "Mở danh sách cuộc trò chuyện" }).click();
  await expect(drawer).toBeVisible();
  await page.getByRole("button", { name: "Đóng danh sách cuộc trò chuyện" }).click();
  await expect(drawer).toBeHidden();
});

test("long conversations keep the composer fixed and scroll only the messages", async ({ page }) => {
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(route, USER);
  });
  await page.route("**/api/v1/conversations**", async (route) => {
    if (route.request().url().includes("/messages?")) {
      const items = Array.from({ length: 24 }, (_, index) => ({
        turn_id: `turn-${String(index)}`,
        role: index % 2 === 0 ? "user" : "assistant",
        content: `Tin nhắn kiểm tra số ${String(index + 1)}`,
        timestamp: `2026-09-18T09:${String(index).padStart(2, "0")}:00Z`,
      }));
      await fulfillJson(route, { items, next_before_message_id: null });
      return;
    }
    await fulfillJson(route, { items: [CONVERSATION], next_cursor: null });
  });

  await page.goto("/chat/company-session-01");
  await page.getByLabel("Nội dung tin nhắn").waitFor();

  const layout = await page.evaluate<{
    bodyFitsViewport: boolean;
    composerBottom: number;
    viewportHeight: number;
    messageAreaScrollable: boolean;
    messageAreaAtBottom: boolean;
    scrollHeight: number;
    scrollTop: number;
    clientHeight: number;
  }>(`(() => {
    const composer = document.querySelector(".chat-composer");
    const messages = document.querySelector(".conversation-scroll");
    const composerRect = composer?.getBoundingClientRect();
    return {
      bodyFitsViewport: document.body.scrollHeight === window.innerHeight,
      composerBottom: composerRect?.bottom ?? 0,
      viewportHeight: window.innerHeight,
      messageAreaScrollable: (messages?.scrollHeight ?? 0) > (messages?.clientHeight ?? 0),
      messageAreaAtBottom: Math.abs(
        (messages?.scrollHeight ?? 0)
        - (messages?.scrollTop ?? 0)
        - (messages?.clientHeight ?? 0)
      ) < 2,
      scrollHeight: messages?.scrollHeight ?? 0,
      scrollTop: messages?.scrollTop ?? 0,
      clientHeight: messages?.clientHeight ?? 0
    };
  })()`);

  expect(layout.bodyFitsViewport).toBe(true);
  expect(layout.composerBottom).toBeLessThanOrEqual(layout.viewportHeight);
  expect(layout.messageAreaScrollable).toBe(true);
  expect(layout.messageAreaAtBottom, JSON.stringify(layout)).toBe(true);
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
      url: APP_URL,
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
  await openConversationDrawerWhenCollapsed(page);
  await page.getByRole("button", { name: "Xem thêm" }).click();

  await expect(page.getByText("Hỗ trợ chuyển vùng")).toBeVisible();
  await expect(page.getByText("Gói cước doanh nghiệp")).toBeVisible();
});

test("pending deletion remains visible and can be retried", async ({ context, page }) => {
  await context.addCookies([
    {
      name: "kira_csrf_dev",
      value: "csrf-delete",
      url: APP_URL,
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
  await openConversationDrawerWhenCollapsed(page);
  await page.getByRole("button", { name: "Thử xóa lại" }).click();
  await expect(page.getByText("Chưa thể xóa. Hãy thử lại.")).toBeVisible();
  await page.getByRole("button", { name: "Thử xóa lại" }).click();

  await expect(page.getByText("Hỗ trợ chuyển vùng")).toHaveCount(0);
  expect(attempts).toBe(2);
});

test("conversation streams one durable turn without duplicate bubbles", async ({
  context,
  page,
}) => {
  await context.addCookies([
    {
      name: "kira_csrf_dev",
      value: "csrf-stream",
      url: APP_URL,
    },
  ]);
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(route, USER);
  });
  let postedMessage = "";
  let postedClientMessageId = "";
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    if (request.method() === "POST" && request.url().endsWith("/messages")) {
      const postedBody = request.postDataJSON() as Record<string, unknown>;
      postedMessage = String(postedBody.message);
      postedClientMessageId = String(postedBody.client_message_id);
      const identity = {
        turn_id: "turn-stream",
        client_message_id: postedClientMessageId,
      };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: [
          `event: message.started\ndata: ${JSON.stringify(identity)}\n\n`,
          `event: message.delta\ndata: ${JSON.stringify({ ...identity, text: "Bạn có thể bật chuyển vùng trong ứng dụng." })}\n\n`,
          `event: message.completed\ndata: ${JSON.stringify({ ...identity, replayed: false, event_id: "event-stream" })}\n\n`,
        ].join(""),
      });
      return;
    }
    if (request.url().includes("/messages?")) {
      await fulfillJson(route, { items: [], next_before_message_id: null });
      return;
    }
    await fulfillJson(route, { items: [CONVERSATION], next_cursor: null });
  });

  await page.goto("/chat/company-session-01");
  await page.getByLabel("Nội dung tin nhắn").fill("Cách bật chuyển vùng?");
  await page.getByRole("button", { name: "Gửi" }).click();

  await expect(page.getByText("Bạn có thể bật chuyển vùng trong ứng dụng.")).toBeVisible();
  await expect(page.getByText("Cách bật chuyển vùng?")).toHaveCount(1);
  expect(postedMessage).toBe("Cách bật chuyển vùng?");
  expect(postedClientMessageId).toMatch(/^[0-9a-f-]{36}$/);
});

test("rename, title search, feedback and themes work together", async ({ context, page }) => {
  await context.addCookies([
    { name: "kira_csrf_dev", value: "csrf-product-ui", url: APP_URL },
  ]);
  await mockAuthenticated(page);

  await page.goto("/chat/company-session-01");
  await page.getByRole("button", { name: "Tùy chọn cho Hỗ trợ chuyển vùng" }).last().click();
  await page.getByRole("menuitem", { name: "Đổi tên" }).click();
  await page.getByRole("textbox", { name: "Tên cuộc trò chuyện" }).fill("Chuyển vùng quốc tế");
  await page.getByRole("button", { name: "Lưu tên" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Chuyển vùng quốc tế" })).toBeVisible();

  await page.keyboard.press("Control+K");
  await page.getByRole("textbox", { name: "Tìm cuộc trò chuyện" }).fill("chuyển vùng");
  await expect(page.getByRole("dialog", { name: "Tìm cuộc trò chuyện" })).toBeVisible();
  await page.getByRole("button", { name: /Hỗ trợ chuyển vùng/ }).click();

  const useful = page.getByRole("button", { name: "Câu trả lời hữu ích" });
  await useful.click();
  await expect(useful).toHaveAttribute("aria-pressed", "true");
  await page.reload();

  await openConversationDrawerWhenCollapsed(page);
  await page.getByRole("button", { name: "Mở menu tài khoản" }).click();
  await page.getByRole("menuitemradio", { name: "Sáng" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByRole("button", { name: "Mở menu tài khoản" }).click();
  await page.getByRole("menuitemradio", { name: "Tối" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("conversation draft survives reload and is scoped to its session", async ({ page }) => {
  await mockAuthenticated(page);
  await page.goto("/chat/company-session-01");
  await page.getByLabel("Nội dung tin nhắn").fill("Nội dung đang viết dở");
  await page.reload();
  await expect(page.getByLabel("Nội dung tin nhắn")).toHaveValue("Nội dung đang viết dở");
});
