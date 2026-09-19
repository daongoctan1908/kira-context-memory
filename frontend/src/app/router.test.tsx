import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";

import { AUTH_SESSION_INVALID_EVENT } from "../api/http";
import { appRoutes } from "./router";

const USER = { user_id: "user-01", username: "alice" };

function renderRoute(path: string) {
  const testRouter = createMemoryRouter(appRoutes, {
    initialEntries: [path],
  });
  return render(<RouterProvider router={testRouter} />);
}

function response(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function authError(code = "AUTH_SESSION_INVALID", status = 401): Response {
  return response(
    {
      code,
      message: "Authentication required",
      correlation_id: "corr-test",
      retryable: status >= 500,
    },
    status,
  );
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.href;
  }
  return input.url;
}

function mockAuthenticatedFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(() => Promise.resolve(response(USER)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  document.cookie = "kira_csrf_dev=; Max-Age=0; Path=/";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("application authentication routes", () => {
  it("redirects an anonymous protected route to login", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(authError())));

    renderRoute("/chat/new");

    expect(
      await screen.findByRole("heading", { name: "Truy cập trợ lý nội bộ" }),
    ).toBeInTheDocument();
  });

  it("logs in without persisting a browser token and restores the intended route", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path.endsWith("/me")) {
        return Promise.resolve(authError());
      }
      if (path.endsWith("/login") && init?.method === "POST") {
        return Promise.resolve(response(USER));
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderRoute("/chat/company-session-01");
    await user.type(await screen.findByLabelText("Tên đăng nhập"), "alice");
    await user.type(screen.getByLabelText("Mật khẩu"), "private-password");
    await user.click(screen.getByRole("button", { name: "Đăng nhập" }));

    expect(await screen.findByText("company-session-01")).toBeInTheDocument();
    const loginCall = fetchMock.mock.calls.find(([input]) => requestPath(input).endsWith("/login"));
    expect(loginCall?.[1]).toMatchObject({
      credentials: "include",
      method: "POST",
    });
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });

  it("rehydrates the user and exposes account actions", async () => {
    mockAuthenticatedFetch();

    renderRoute("/chat/new");

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Đổi mật khẩu/ })).toHaveAttribute(
      "href",
      "/account/password",
    );
    expect(screen.getByRole("button", { name: "Đăng xuất" })).toBeEnabled();
  });

  it("shows dependency failure separately and can retry rehydration", async () => {
    let attempt = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        attempt += 1;
        return Promise.resolve(
          attempt === 1 ? authError("AUTH_UNAVAILABLE", 503) : response(USER),
        );
      }),
    );
    const user = userEvent.setup();

    renderRoute("/chat/new");
    await user.click(await screen.findByRole("button", { name: "Thử lại" }));

    expect(await screen.findByRole("heading", { name: "Bạn muốn hỏi gì?" })).toBeInTheDocument();
  });

  it("sends CSRF on password change and requires login again", async () => {
    document.cookie = "kira_csrf_dev=csrf%20value; Path=/";
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      return Promise.resolve(
        requestPath(input).endsWith("/me") && init?.body === undefined
          ? response(USER)
          : response(null, 204),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderRoute("/account/password");
    await user.type(await screen.findByLabelText("Mật khẩu hiện tại"), "old-password-123");
    await user.type(screen.getByLabelText("Mật khẩu mới"), "new-password-123");
    await user.type(screen.getByLabelText("Nhập lại mật khẩu mới"), "new-password-123");
    await user.click(screen.getByRole("button", { name: "Đổi mật khẩu" }));

    expect(await screen.findByText("Mật khẩu đã được đổi. Hãy đăng nhập lại.")).toBeVisible();
    const changeCall = fetchMock.mock.calls.find(([input]) =>
      requestPath(input).endsWith("/change-password"),
    );
    const headers = changeCall?.[1]?.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("csrf value");
    expect(changeCall?.[1]).toMatchObject({ credentials: "include", method: "POST" });
  });

  it("keeps the authenticated UI when logout cannot reach the backend", async () => {
    document.cookie = "kira_csrf_dev=csrf-value; Path=/";
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        if (requestPath(input).endsWith("/me")) {
          return Promise.resolve(response(USER));
        }
        return Promise.reject(new TypeError("offline detail"));
      }),
    );
    const user = userEvent.setup();

    renderRoute("/chat/new");
    await user.click(await screen.findByRole("button", { name: "Đăng xuất" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Không thể kết nối tới máy chủ");
    expect(screen.getByRole("heading", { name: "Bạn muốn hỏi gì?" })).toBeVisible();
  });

  it("sends CSRF on logout and returns to the login screen", async () => {
    document.cookie = "kira_csrf_dev=logout-token; Path=/";
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      Promise.resolve(
        requestPath(input).endsWith("/me") && init?.body === undefined
          ? response(USER)
          : response(null, 204),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderRoute("/chat/new");
    await user.click(await screen.findByRole("button", { name: "Đăng xuất" }));

    expect(await screen.findByText("Bạn đã đăng xuất an toàn.")).toBeVisible();
    const logoutCall = fetchMock.mock.calls.find(([input]) =>
      requestPath(input).endsWith("/logout"),
    );
    const headers = logoutCall?.[1]?.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("logout-token");
  });

  it("returns an authenticated route to login when the session expires", async () => {
    mockAuthenticatedFetch();

    renderRoute("/chat/new");
    expect(await screen.findByRole("heading", { name: "Bạn muốn hỏi gì?" })).toBeVisible();

    act(() => {
      window.dispatchEvent(new Event(AUTH_SESSION_INVALID_EVENT));
    });

    expect(
      await screen.findByRole("heading", { name: "Truy cập trợ lý nội bộ" }),
    ).toBeVisible();
  });

  it("renders an unknown public route without waiting on auth", async () => {
    const fetchMock = mockAuthenticatedFetch();

    renderRoute("/missing");

    expect(screen.getByRole("heading", { name: "Đường dẫn không tồn tại" })).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
  });
});
