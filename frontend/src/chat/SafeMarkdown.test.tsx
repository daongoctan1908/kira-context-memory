import { fireEvent, render, screen } from "@testing-library/react";

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

  it("preserves Kira's positive and negative values, including nested emphasis", () => {
    const { container } = render(
      <SafeMarkdown content={
        "Tăng: <span style='color:green'>**0,11%** (3,1 triệu)</span>\n\nGiảm: **<span style='color:red'>-19,81%</span>**"
      } />,
    );

    const positive = container.querySelector(".kira-value-positive");
    const negative = container.querySelector(".kira-value-negative");
    expect(positive).toHaveTextContent("0,11% (3,1 triệu)");
    expect(positive?.querySelector("strong")).toHaveTextContent("0,11%");
    expect(negative).toHaveTextContent("-19,81%");
    expect(negative?.closest("strong")).not.toBeNull();
    expect(container.querySelector("[style]")).toBeNull();
  });

  it("keeps an unfinished streamed color value readable until the closing span arrives", () => {
    const { container, rerender } = render(
      <SafeMarkdown content={"Doanh thu tăng <span style='color:green'>1,85%"} />,
    );

    expect(container.querySelector(".kira-value-positive")).toHaveTextContent("1,85%");
    expect(container).not.toHaveTextContent("<span");

    rerender(<SafeMarkdown content={"Doanh thu tăng <span style='color:green'>1,85%</span> (0,12 tỷ)"} />);
    expect(container.querySelector(".kira-value-positive")).toHaveTextContent("1,85%");
    expect(container.querySelector(".kira-value-positive")).not.toHaveTextContent("0,12 tỷ");
  });

  it("hides incomplete HTML tag fragments while a Kira value streams", () => {
    const { container, rerender } = render(
      <SafeMarkdown content={"Tăng <span style='color:green"} />,
    );

    expect(container.textContent.trim()).toBe("Tăng");
    expect(container).not.toHaveTextContent("<span");

    rerender(<SafeMarkdown content={"Tăng <span style='color:green'>1,85%</spa"} />);
    expect(container.querySelector(".kira-value-positive")).toHaveTextContent("1,85%");
    expect(container).not.toHaveTextContent("</spa");

    rerender(<SafeMarkdown content={"Tăng <span style='color:green'>1,85%</span> (0,12 tỷ)"} />);
    expect(container.textContent).toBe("Tăng 1,85% (0,12 tỷ)");
    expect(container.querySelector(".kira-value-positive")?.textContent).toBe("1,85%");
  });

  it("preserves an escaped incomplete span as literal Markdown text", () => {
    const { container } = render(<SafeMarkdown content={"Ví dụ \\<span"} />);

    expect(container.textContent).toBe("Ví dụ <span");
    expect(container.querySelector(".kira-value-positive, .kira-value-negative")).toBeNull();
  });

  it("does not promote arbitrary HTML or inline styles into executable markup", () => {
    const { container } = render(
      <SafeMarkdown content={
        "<span style='color:green;position:fixed' onclick='alert(1)'>1,85%</span>\n\n<div data-private='secret'>Nội dung</div>\n\n<script>alert('x')</script>\n\n<img src='https://evil.example/pixel' onerror='alert(1)'>"
      } />,
    );

    expect(container.querySelector("script, img, [onclick], [onerror], [style], [data-private]")).toBeNull();
    expect(container).not.toHaveTextContent("alert");
    expect(container.querySelector(".kira-value-positive")).toHaveTextContent("1,85%");
  });

  it("leaves HTML examples in inline and fenced code untouched", () => {
    const html = "<span style='color:green'>1,85%</span>";
    const { container } = render(
      <SafeMarkdown content={`Ví dụ: \`${html}\`\n\n\`\`\`html\n${html}\n\`\`\``} />,
    );

    expect(container.querySelector("p code")).toHaveTextContent(html);
    expect(container.querySelector("pre code")).toHaveTextContent(html);
    expect(container.querySelector(".kira-value-positive")).toBeNull();
  });

  it("makes GFM data tables accessible and keeps values in their columns", () => {
    render(<SafeMarkdown content={
      "| Địa bàn | Tăng trưởng |\n| :--- | ---: |\n| **Phú Thọ** | <span style='color:red'>-1,43%</span> |"
    } />);

    const region = screen.getByRole("region", { name: "Bảng số liệu" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region.querySelector("table")).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "Tăng trưởng" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "-1,43%" }).querySelector(".kira-value-negative")).not.toBeNull();
  });

  it.each([
    "chart_image_url_placeholder",
    "javascript:alert(1)",
    "https://evil.example/chart.png",
  ])("shows a readable chart placeholder for unsupported image URL %s", (src) => {
    const { container } = render(<SafeMarkdown content={`![Biểu đồ doanh thu](${src})`} />);

    expect(screen.getByText("Biểu đồ chưa khả dụng")).toBeInTheDocument();
    expect(container.querySelector(".message-image-unavailable")).not.toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders a permitted chart with its caption and handles a broken image", () => {
    const { container, rerender } = render(<SafeMarkdown content={
      "![Biểu đồ doanh thu](https://charts.vietteltelecom.vn/chart.png)"
    } />);

    const image = screen.getByRole("img", { name: "Biểu đồ doanh thu" });
    expect(image.closest(".message-image")).not.toBeNull();
    expect(screen.getByText("Biểu đồ doanh thu")).toBeInTheDocument();
    fireEvent.error(image);
    expect(screen.getByText("Biểu đồ chưa khả dụng")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();

    rerender(<SafeMarkdown content={
      "![Biểu đồ doanh thu](https://charts.vietteltelecom.vn/chart.png)\n\nNhận xét đang được cập nhật."
    } />);
    expect(screen.getByText("Biểu đồ chưa khả dụng")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();

    rerender(<SafeMarkdown content={
      "![Biểu đồ mới](https://charts.vietteltelecom.vn/chart-updated.png)"
    } />);
    expect(screen.getByRole("img", { name: "Biểu đồ mới" })).toHaveAttribute(
      "src", "https://charts.vietteltelecom.vn/chart-updated.png",
    );
    expect(screen.queryByText("Biểu đồ chưa khả dụng")).not.toBeInTheDocument();
  });
});
