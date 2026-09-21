import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";

import { appRoutes } from "../app/router";

const USER = { user_id: "user-01", username: "alice" };
const ACTIVE_CONVERSATION = {
  conversation_id: "11111111-1111-4111-8111-111111111111",
  session_id: "session-one",
  title: "Hỗ trợ chuyển vùng",
  status: "active",
  created_at: "2026-09-18T08:00:00Z",
  updated_at: "2026-09-18T08:00:00Z",
  last_message_at: "2026-09-18T09:00:00Z",
};

function renderRoute(path: string) {
  const router = createMemoryRouter(appRoutes, { initialEntries: [path] });
  return render(<RouterProvider router={router} />);
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function pathOf(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.href;
  }
  return input.url;
}

function gatewayError(code: string, status: number): Response {
  return jsonResponse(
    {
      code,
      message: "Request failed",
      correlation_id: "corr-conversation",
      retryable: status >= 500,
    },
    status,
  );
}

function streamFrame(name: string, payload: Record<string, unknown>): string {
  return `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
}

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

beforeEach(() => {
  document.cookie = "kira_csrf_dev=csrf-conversation; Path=/";
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("conversation management UI", () => {
  it("paginates the sidebar without duplicating conversations", async () => {
    const second = {
      ...ACTIVE_CONVERSATION,
      conversation_id: "22222222-2222-4222-8222-222222222222",
      session_id: "session-two",
      title: "Gói cước doanh nghiệp",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path.endsWith("/me")) {
          return Promise.resolve(jsonResponse(USER));
        }
        if (path.includes("cursor=next-page")) {
          return Promise.resolve(jsonResponse({ items: [second], next_cursor: null }));
        }
        if (path.includes("/api/v1/conversations?")) {
          return Promise.resolve(
            jsonResponse({ items: [ACTIVE_CONVERSATION], next_cursor: "next-page" }),
          );
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      }),
    );
    const user = userEvent.setup();

    renderRoute("/chat/new");
    expect(await screen.findByText("Hỗ trợ chuyển vùng")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Xem thêm" }));

    expect(await screen.findByText("Gói cước doanh nghiệp")).toBeVisible();
    expect(screen.getAllByText("Hỗ trợ chuyển vùng")).toHaveLength(1);
  });

  it("creates a titled conversation with CSRF and opens its public session route", async () => {
    let createCsrf: string | null = null;
    let createBody: unknown;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = pathOf(input);
      if (path.endsWith("/me")) {
        return Promise.resolve(jsonResponse(USER));
      }
      if (path.includes("/api/v1/conversations?") && !path.includes("/messages?")) {
        return Promise.resolve(jsonResponse({ items: [], next_cursor: null }));
      }
      if (path.endsWith("/api/v1/conversations") && init?.method === "POST") {
        createCsrf = (init.headers as Headers).get("X-CSRF-Token");
        createBody = typeof init.body === "string" ? JSON.parse(init.body) as unknown : null;
        return Promise.resolve(jsonResponse(ACTIVE_CONVERSATION, 201));
      }
      if (path.includes("/session-one/messages?")) {
        return Promise.resolve(jsonResponse({ items: [], next_before_message_id: null }));
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderRoute("/chat/new");
    await user.type(await screen.findByLabelText("Tin nhắn đầu tiên"), "Hỗ trợ chuyển vùng");
    await user.click(screen.getByRole("button", { name: "Bắt đầu trò chuyện" }));

    expect(await screen.findByRole("heading", { name: "Hỗ trợ chuyển vùng" })).toBeVisible();
    expect(createCsrf).toBe("csrf-conversation");
    expect(createBody).toEqual({ title: "Hỗ trợ chuyển vùng" });
  });

  it("creates a conversation from the first prompt and sends it once", async () => {
    const clientMessageId = "33333333-3333-4333-8333-333333333333";
    const prompt = "Hướng dẫn bật chuyển vùng quốc tế";
    let createBody: unknown;
    let sentBody: unknown;
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(clientMessageId);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = pathOf(input);
        if (path.endsWith("/me")) {
          return Promise.resolve(jsonResponse(USER));
        }
        if (path.endsWith("/api/v1/conversations") && init?.method === "POST") {
          createBody = typeof init.body === "string" ? JSON.parse(init.body) as unknown : null;
          return Promise.resolve(jsonResponse({ ...ACTIVE_CONVERSATION, title: prompt }, 201));
        }
        if (path.endsWith("/session-one/messages") && init?.method === "POST") {
          sentBody = typeof init.body === "string" ? JSON.parse(init.body) as unknown : null;
          const identity = { turn_id: "turn-initial", client_message_id: clientMessageId };
          return Promise.resolve(
            streamResponse([
              streamFrame("message.started", identity),
              streamFrame("message.delta", { ...identity, text: "Bạn hãy mở phần chuyển vùng." }),
              streamFrame("message.completed", { ...identity, replayed: false }),
            ]),
          );
        }
        if (path.includes("/session-one/messages?")) {
          return Promise.resolve(jsonResponse({ items: [], next_before_message_id: null }));
        }
        if (path.includes("/api/v1/conversations?")) {
          return Promise.resolve(jsonResponse({ items: [], next_cursor: null }));
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      }),
    );
    const user = userEvent.setup();

    renderRoute("/chat/new");
    await user.type(await screen.findByLabelText("Tin nhắn đầu tiên"), prompt);
    await user.click(screen.getByRole("button", { name: "Bắt đầu trò chuyện" }));

    expect(await screen.findByText("Bạn hãy mở phần chuyển vùng.")).toBeVisible();
    expect(createBody).toEqual({ title: prompt });
    expect(sentBody).toEqual({ message: prompt, client_message_id: clientMessageId });
    expect(screen.getAllByText(prompt).length).toBeGreaterThanOrEqual(2);
  });

  it("keeps new messages in view and promotes the active conversation", async () => {
    const clientMessageId = "44444444-4444-4444-8444-444444444444";
    const newerConversation = {
      ...ACTIVE_CONVERSATION,
      conversation_id: "22222222-2222-4222-8222-222222222222",
      session_id: "session-two",
      title: "Cuộc trò chuyện đang ở đầu",
      updated_at: "2026-09-19T08:00:00Z",
      last_message_at: "2026-09-19T09:00:00Z",
    };
    const identity = {
      turn_id: "turn-live",
      client_message_id: clientMessageId,
    };
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(clientMessageId);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(900);
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = pathOf(input);
        if (path.endsWith("/me")) {
          return Promise.resolve(jsonResponse(USER));
        }
        if (path.endsWith("/session-one/messages") && init?.method === "POST") {
          return Promise.resolve(
            streamResponse([
              streamFrame("message.started", identity),
              streamFrame("message.delta", { ...identity, text: "Đã trả lời" }),
              streamFrame("message.completed", { ...identity, replayed: false }),
            ]),
          );
        }
        if (path.includes("/session-one/messages?")) {
          return Promise.resolve(jsonResponse({ items: [], next_before_message_id: null }));
        }
        if (path.includes("/api/v1/conversations?")) {
          return Promise.resolve(
            jsonResponse({
              items: [newerConversation, ACTIVE_CONVERSATION],
              next_cursor: null,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      }),
    );
    const user = userEvent.setup();

    const rendered = renderRoute("/chat/session-one");
    await screen.findByText("Cuộc trò chuyện đang ở đầu");
    const conversationList = rendered.container.querySelector<HTMLElement>(".conversation-history");
    expect(conversationList).not.toBeNull();
    if (conversationList === null) {
      return;
    }
    expect(within(conversationList).getAllByRole("link")[0]).toHaveTextContent(
      "Cuộc trò chuyện đang ở đầu",
    );
    const viewport = rendered.container.querySelector<HTMLElement>(".conversation-scroll");
    expect(viewport).not.toBeNull();
    if (viewport === null) {
      return;
    }
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 300 });
    const scrollTo = vi.fn();
    viewport.scrollTo = scrollTo;
    await waitFor(() => {
      expect(viewport.scrollTop).toBe(900);
    });
    viewport.scrollTop = 0;
    fireEvent.scroll(viewport);
    await user.click(screen.getByRole("button", { name: "Cuộn xuống tin nhắn mới nhất" }));
    expect(scrollTo).toHaveBeenCalledWith({ top: 900, behavior: "smooth" });

    await user.type(screen.getByLabelText("Nội dung tin nhắn"), "Câu hỏi mới");
    await user.click(screen.getByRole("button", { name: "Gửi" }));

    expect(await screen.findByText("Đã trả lời")).toBeVisible();
    await waitFor(() => {
      expect(viewport.scrollTop).toBe(900);
      expect(within(conversationList).getAllByRole("link")[0]).toHaveTextContent(
        "Hỗ trợ chuyển vùng",
      );
    });
  });

  it("confirms deletion and removes only the selected conversation", async () => {
    const remaining = {
      ...ACTIVE_CONVERSATION,
      conversation_id: "33333333-3333-4333-8333-333333333333",
      session_id: "session-remaining",
      title: "Giữ lại cuộc trò chuyện này",
    };
    let deleteCsrf: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = pathOf(input);
        if (path.endsWith("/me")) {
          return Promise.resolve(jsonResponse(USER));
        }
        if (path.includes("/api/v1/conversations?")) {
          return Promise.resolve(
            jsonResponse({ items: [ACTIVE_CONVERSATION, remaining], next_cursor: null }),
          );
        }
        if (path.endsWith("/session-one") && init?.method === "DELETE") {
          deleteCsrf = (init.headers as Headers).get("X-CSRF-Token");
          return Promise.resolve(jsonResponse(null, 204));
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      }),
    );
    const user = userEvent.setup();

    renderRoute("/chat/new");
    await user.click(
      await screen.findByRole("button", { name: "Tùy chọn cho Hỗ trợ chuyển vùng" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Xóa" }));
    expect(screen.getByRole("dialog", { name: "Xóa cuộc trò chuyện?" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Xóa vĩnh viễn" }));

    await waitFor(() => {
      expect(screen.queryByText("Hỗ trợ chuyển vùng")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Giữ lại cuộc trò chuyện này")).toBeVisible();
    expect(deleteCsrf).toBe("csrf-conversation");
  });

  it("keeps a failed deletion pending and retries the idempotent request", async () => {
    const pending = { ...ACTIVE_CONVERSATION, status: "deletion_pending" };
    let deleteAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path = pathOf(input);
        if (path.endsWith("/me")) {
          return Promise.resolve(jsonResponse(USER));
        }
        if (path.includes("/api/v1/conversations?")) {
          return Promise.resolve(jsonResponse({ items: [pending], next_cursor: null }));
        }
        if (path.endsWith("/session-one") && init?.method === "DELETE") {
          deleteAttempts += 1;
          return Promise.resolve(
            deleteAttempts === 1
              ? gatewayError("DELETION_RETRY_REQUIRED", 503)
              : jsonResponse(null, 204),
          );
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      }),
    );
    const user = userEvent.setup();

    renderRoute("/chat/new");
    await user.click(await screen.findByRole("button", { name: "Thử xóa lại" }));
    expect(await screen.findByText("Chưa thể xóa. Hãy thử lại.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Thử xóa lại" }));

    await waitFor(() => {
      expect(screen.queryByText("Hỗ trợ chuyển vùng")).not.toBeInTheDocument();
    });
    expect(deleteAttempts).toBe(2);
  });

  it("prepends older history and handles an inaccessible conversation as not found", async () => {
    let inaccessible = false;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path.endsWith("/me")) {
          return Promise.resolve(jsonResponse(USER));
        }
        if (path.includes("/api/v1/conversations?") && !path.includes("/messages?")) {
          return Promise.resolve(
            jsonResponse({ items: [ACTIVE_CONVERSATION], next_cursor: null }),
          );
        }
        if (path.includes("/missing/messages?")) {
          inaccessible = true;
          return Promise.resolve(gatewayError("CONVERSATION_NOT_FOUND", 404));
        }
        if (path.includes("before_message_id=41")) {
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  turn_id: "turn-old",
                  role: "user",
                  content: "Tin nhắn cũ",
                  timestamp: "2026-09-17T08:00:00Z",
                },
              ],
              next_before_message_id: null,
            }),
          );
        }
        if (path.includes("/session-one/messages?")) {
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  turn_id: "turn-new",
                  role: "assistant",
                  content: "Tin nhắn mới",
                  timestamp: "2026-09-18T08:00:00Z",
                },
              ],
              next_before_message_id: 41,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      }),
    );
    const user = userEvent.setup();

    const rendered = renderRoute("/chat/session-one");
    await user.click(await screen.findByRole("button", { name: "Tải tin nhắn cũ hơn" }));
    const history = screen.getByRole("list", { name: "Tin nhắn trong cuộc trò chuyện" });
    expect(within(history).getAllByText(/Tin nhắn/).map((node) => node.textContent)).toEqual([
      "Tin nhắn cũ",
      "Tin nhắn mới",
    ]);

    rendered.unmount();
    renderRoute("/chat/missing");
    expect(await screen.findByText("Không thể hoàn tất yêu cầu. Hãy thử lại.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Về cuộc trò chuyện mới" })).toBeVisible();
    expect(inaccessible).toBe(true);
  });
});
