import { readFileSync } from "node:fs";
import { expect, test, type Page, type Route } from "@playwright/test";

interface DatasetMessage {
  role: string;
  text: string;
  fill_id?: string;
}

interface DatasetBundle {
  conversation: Record<string, string | DatasetMessage[]>;
}

// Keep these fixtures tied to actual Kira answers, rather than simplified copies.
const datasetMessages = ["conv01", "conv04"].flatMap((bundleName) => {
  const bundle = JSON.parse(readFileSync(
    new URL(`../../../dataset/kira_ltm_v1/bundles/${bundleName}/conversation.json`, import.meta.url),
    "utf8",
  )) as DatasetBundle;
  return Object.values(bundle.conversation)
    .flatMap((session) => typeof session === "string" ? [] : session);
});

function answer(fillId: string): string {
  const message = datasetMessages.find((item) => item.role === "assistant" && item.fill_id === fillId);
  if (message === undefined) throw new Error(`Missing Kira dataset answer ${fillId}`);
  return message.text;
}

const CONVERSATION = {
  conversation_id: "11111111-1111-4111-8111-111111111111",
  session_id: "kira-dataset-rendering",
  title: "Báo cáo kinh doanh",
  status: "active",
  created_at: "2026-09-18T08:00:00Z",
  updated_at: "2026-09-18T08:00:00Z",
  last_message_at: "2026-09-18T09:00:00Z",
};

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
}

async function openDatasetAnswer(page: Page, fillId: string, theme: "light" | "dark") {
  await page.addInitScript({ content: `window.localStorage.setItem("kira-theme", "${theme}");` });
  await page.route("**/api/v1/auth/me", async (route) => {
    await fulfillJson(route, { user_id: "user-01", username: "alice" });
  });
  await page.route("**/api/v1/conversations**", async (route) => {
    if (route.request().url().includes("/messages?")) {
      await fulfillJson(route, {
        items: [{
          turn_id: fillId,
          role: "assistant",
          content: answer(fillId),
          timestamp: "2026-09-18T09:00:01Z",
          feedback: null,
        }],
        next_before_message_id: null,
      });
      return;
    }
    await fulfillJson(route, { items: [CONVERSATION], next_cursor: null });
  });
  await page.goto(`/chat/${CONVERSATION.session_id}`);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  await expect(page.getByRole("table")).toBeVisible();
}

async function verifyTableLayout(page: Page) {
  const region = page.getByRole("region", { name: "Bảng số liệu" });
  await expect(region).toHaveAttribute("tabindex", "0");
  const layout = await page.evaluate<{
    viewportWidth: number;
    documentWidth: number;
    containerWidth: number;
    contentWidth: number;
  }>(`(() => {
    const element = document.querySelector(".message-table-scroll");
    return {
      viewportWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      containerWidth: element.clientWidth,
      contentWidth: element.scrollWidth,
    };
  })()`);
  expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth);
  if (layout.viewportWidth <= 767) {
    expect(layout.contentWidth).toBeGreaterThan(layout.containerWidth);
    await page.evaluate(`(() => {
      const element = document.querySelector(".message-table-scroll");
      element.scrollLeft = element.scrollWidth;
    })()`);
    expect(await page.evaluate<number>("document.querySelector('.message-table-scroll').scrollLeft")).toBeGreaterThan(0);
    await page.evaluate("document.querySelector('.message-table-scroll').scrollLeft = 0");
  }
}

for (const theme of ["light", "dark"] as const) {
  test(`dataset daily comparison retains nested emphasis and signed colors in ${theme}`, async ({ page }, testInfo) => {
    await openDatasetAnswer(page, "CONV01V2_FILL_009", theme);

    await expect(page.getByRole("columnheader", { name: "Doanh thu tiêu dùng di động", exact: true })).toBeVisible();
    const negative = page.locator(".kira-value-negative").first();
    const positive = page.locator(".kira-value-positive").first();
    await expect(negative).toHaveText("-1,43% (-0,07 tỷ)");
    await expect(negative.locator("strong")).toHaveText("-1,43%");
    await expect(positive).toHaveText("0,11% (3,1 triệu)");
    expect(await page.evaluate<string>("getComputedStyle(document.querySelector('.kira-value-positive')).color"))
      .not.toBe(await page.evaluate<string>("getComputedStyle(document.querySelector('.kira-value-negative')).color"));
    await expect(page.locator(".message-markdown")).not.toContainText("<span");
    await verifyTableLayout(page);
    await page.screenshot({ path: testInfo.outputPath(`kira-comparison-${theme}.png`) });
  });

  test(`dataset wide cumulative table stays horizontally scrollable in ${theme}`, async ({ page }, testInfo) => {
    await openDatasetAnswer(page, "CONV01V2_FILL_004", theme);

    await expect(page.getByRole("columnheader")).toHaveCount(6);
    await expect(page.getByRole("cell", { name: "33.261 TB", exact: true })).toHaveCount(2);
    await expect(page.locator(".kira-value-negative").filter({ hasText: "-19,81%" })).toHaveCount(2);
    await expect(page.locator(".kira-value-positive").filter({ hasText: "4,87%" })).toHaveCount(1);
    await verifyTableLayout(page);
    await page.screenshot({ path: testInfo.outputPath(`kira-cumulative-${theme}.png`) });
  });

  test(`dataset Top 3 table has a readable unavailable chart in ${theme}`, async ({ page }, testInfo) => {
    const requestedPlaceholders: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("chart_image_url_placeholder")) requestedPlaceholders.push(request.url());
    });
    await openDatasetAnswer(page, "CONV01V2_FILL_035", theme);

    await expect(page.getByRole("row")).toHaveCount(4);
    await expect(page.getByRole("cell", { name: "3.213.189 TB", exact: true })).toBeVisible();
    await expect(page.getByText("Biểu đồ chưa khả dụng", { exact: true })).toBeVisible();
    await expect(page.locator(".message-image-unavailable")).toHaveCount(1);
    await expect(page.locator(".message-markdown img")).toHaveCount(0);
    expect(requestedPlaceholders).toEqual([]);
    await verifyTableLayout(page);
    await page.screenshot({ path: testInfo.outputPath(`kira-top-three-${theme}.png`) });
  });
}

const CHART_CAPTION = "Top 3 tỉnh xếp hạng đầu về Thuê bao FTTH rời mạng tháng 08/2026";
// Local chart response keeps rendering tests deterministic and avoids external requests.
const CHART_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="900" height="340" viewBox="0 0 900 340">
  <rect width="900" height="340" fill="#fff"/>
  <g font-family="Arial, sans-serif" fill="#252220">
    <text x="30" y="40" font-size="22" font-weight="700">Thuê bao FTTH rời mạng · Tháng 08/2026</text>
    <text x="30" y="113" font-size="18">TP. Hồ Chí Minh</text>
    <text x="30" y="193" font-size="18">TP. Hà Nội</text>
    <text x="30" y="273" font-size="18">Đồng Nai</text>
    <rect x="210" y="80" width="570" height="50" rx="5" fill="#cf2637"/>
    <rect x="210" y="160" width="285" height="50" rx="5" fill="#cf2637"/>
    <rect x="210" y="240" width="130" height="50" rx="5" fill="#cf2637"/>
    <text x="792" y="113" font-size="18">12.504</text>
    <text x="507" y="193" font-size="18">6.254</text>
    <text x="352" y="273" font-size="18">2.849</text>
  </g>
</svg>`;

test("dataset chart URL renders a captioned image within the chat width", async ({ page }, testInfo) => {
  const requests: string[] = [];
  await page.route("https://charts.vietteltelecom.vn/**", async (route) => {
    requests.push(route.request().url());
    await route.fulfill({ contentType: "image/svg+xml", body: CHART_SVG });
  });
  await openDatasetAnswer(page, "CONV04_FILL_009", "light");

  const image = page.getByRole("img", { name: CHART_CAPTION });
  await image.scrollIntoViewIfNeeded();
  await expect(image).toBeVisible();
  await expect.poll(async () => page.evaluate<boolean>(
    "(() => { const image = document.querySelector('.message-image img'); return image.complete && image.naturalWidth > 0; })()",
  )).toBe(true);
  await page.locator(".message-image").scrollIntoViewIfNeeded();
  await expect(page.locator(".message-image-caption")).toHaveText(CHART_CAPTION);
  await expect(page.locator(".message-image-caption")).toBeInViewport();
  await expect(page.locator(".message-image-unavailable")).toHaveCount(0);
  expect(requests).toHaveLength(1);
  expect(requests[0]).toContain("https://charts.vietteltelecom.vn/chart?data=");
  const bounds = await page.evaluate<{
    imageWidth: number;
    containerWidth: number;
    documentWidth: number;
    viewportWidth: number;
  }>(`(() => {
    const element = document.querySelector(".message-image img");
    return {
      imageWidth: element.getBoundingClientRect().width,
      containerWidth: element.parentElement.getBoundingClientRect().width,
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    };
  })()`);
  expect(bounds.imageWidth).toBeLessThanOrEqual(bounds.containerWidth);
  expect(bounds.documentWidth).toBeLessThanOrEqual(bounds.viewportWidth);
  await page.screenshot({ path: testInfo.outputPath("kira-chart-success.png") });
});

test("dataset chart URL falls back to its caption when the image request fails", async ({ page }, testInfo) => {
  let requests = 0;
  await page.route("https://charts.vietteltelecom.vn/**", async (route) => {
    requests += 1;
    await route.abort();
  });
  await openDatasetAnswer(page, "CONV04_FILL_009", "light");

  const chart = page.locator(".message-image");
  await chart.scrollIntoViewIfNeeded();
  await expect(page.getByText("Biểu đồ chưa khả dụng", { exact: true })).toBeVisible();
  await expect(chart).toHaveClass(/message-image-unavailable/);
  await expect(chart.locator(".message-image-caption")).toHaveText(CHART_CAPTION);
  await expect(page.locator(".message-markdown img")).toHaveCount(0);
  expect(requests).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("kira-chart-failure.png") });
});
