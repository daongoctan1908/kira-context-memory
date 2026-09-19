import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";

import { appRoutes } from "./router";

function renderRoute(path: string) {
  const testRouter = createMemoryRouter(appRoutes, {
    initialEntries: [path],
  });
  return render(<RouterProvider router={testRouter} />);
}

describe("application routes", () => {
  it("renders the login route", () => {
    renderRoute("/login");

    expect(
      screen.getByRole("heading", { name: "Truy cập trợ lý nội bộ" }),
    ).toBeInTheDocument();
  });

  it("renders the new chat route inside the application shell", () => {
    renderRoute("/chat/new");

    expect(screen.getByRole("heading", { name: "Bạn muốn hỏi gì?" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Điều hướng cuộc trò chuyện" })).toBeInTheDocument();
  });

  it("renders a conversation route with its public session identifier", () => {
    renderRoute("/chat/conv-2026-09");

    expect(screen.getByRole("heading", { name: "Cuộc trò chuyện" })).toBeInTheDocument();
    expect(screen.getByText("conv-2026-09")).toBeInTheDocument();
  });

  it("renders a recovery link for unknown routes", () => {
    renderRoute("/missing");

    expect(screen.getByRole("heading", { name: "Đường dẫn không tồn tại" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Mở cuộc trò chuyện mới" })).toHaveAttribute(
      "href",
      "/chat/new",
    );
  });
});
