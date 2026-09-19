import { render, screen } from "@testing-library/react";

import { SafeMarkdown } from "./SafeMarkdown";

describe("safe Markdown", () => {
  it("renders basic Markdown without mounting raw HTML", () => {
    const { container } = render(
      <SafeMarkdown content={'Nội dung **quan trọng**.\n\n<script>alert("x")</script><img src=x onerror=alert(1)>'} />,
    );

    expect(screen.getByText("quan trọng")).toHaveProperty("tagName", "STRONG");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).not.toContain("alert");
  });
});
