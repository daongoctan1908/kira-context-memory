import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChatComposer } from "./ChatComposer";
import { CopyMessageButton } from "./CopyMessageButton";
import { useChatStream } from "./useChatStream";

const CLIENT_MESSAGE_ID = "55555555-5555-4555-8555-555555555555";

function frame(name: string, payload: Record<string, unknown>): string {
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

function Harness() {
  const chat = useChatStream({ sessionId: "session-one" });
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          chat.send("Câu hỏi thử nghiệm");
        }}
      >
        Gửi mẫu
      </button>
      <button type="button" onClick={chat.stop}>Dừng mẫu</button>
      <p data-testid="turn-count">{chat.turns.length}</p>
      {chat.turns.map((turn) => (
        <section key={turn.clientMessageId}>
          <p>{turn.userText}</p>
          <p>{turn.assistantText}</p>
          <p>{turn.status}</p>
          {turn.retryable ? (
            <button
              type="button"
              onClick={() => {
                chat.retry(turn.clientMessageId);
              }}
            >
              Thử gửi lại
            </button>
          ) : null}
        </section>
      ))}
    </div>
  );
}

beforeEach(() => {
  document.cookie = "kira_csrf_dev=stream-token; Path=/";
  vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(CLIENT_MESSAGE_ID);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("chat composer", () => {
  it("sends with Enter, preserves Shift + Enter, and exposes stop while streaming", async () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <ChatComposer sessionId="test-session" streaming={false} onSend={onSend} onStop={onStop} />,
    );

    const input = screen.getByLabelText("Nội dung tin nhắn");
    await user.type(input, "Dòng một{shift>}{enter}{/shift}Dòng hai");
    expect(input).toHaveValue("Dòng một\nDòng hai");
    await user.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith("Dòng một\nDòng hai");
    expect(input).toHaveValue("");

    rerender(<ChatComposer sessionId="test-session" streaming onSend={onSend} onStop={onStop} />);
    await user.click(screen.getByRole("button", { name: "Dừng" }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("copies an assistant answer without exposing it elsewhere", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<CopyMessageButton content="Nội dung cần sao chép" />);

    await user.click(screen.getByRole("button", { name: "Sao chép" }));

    expect(writeText).toHaveBeenCalledWith("Nội dung cần sao chép");
    expect(screen.getByRole("button", { name: "Đã sao chép" })).toBeVisible();
  });
});

describe("chat stream state", () => {
  it("retries with the same client ID and replaces the unsaved answer", async () => {
    const identity = { turn_id: "turn-05", client_message_id: CLIENT_MESSAGE_ID };
    const first = streamResponse([
      frame("message.started", identity),
      frame("message.delta", { ...identity, text: "Bản chưa lưu" }),
    ]);
    const second = streamResponse([
      frame("message.started", identity),
      frame("message.delta", { ...identity, text: "Bản đã lưu" }),
      frame("message.completed", { ...identity, replayed: true }),
    ]);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Gửi mẫu" }));
    expect(await screen.findByText("unsaved")).toBeInTheDocument();
    expect(screen.getByTestId("turn-count")).toHaveTextContent("1");

    await user.click(screen.getByRole("button", { name: "Thử gửi lại" }));
    expect(await screen.findByText("completed")).toBeInTheDocument();
    expect(screen.getByText("Bản đã lưu")).toBeInTheDocument();
    expect(screen.queryByText("Bản chưa lưu")).not.toBeInTheDocument();
    expect(screen.getAllByText("Câu hỏi thử nghiệm")).toHaveLength(1);
    expect(screen.getByTestId("turn-count")).toHaveTextContent("1");

    const requestBodies = fetchMock.mock.calls.map(([, init]) => {
      const body = (init as RequestInit | undefined)?.body;
      return typeof body === "string" ? JSON.parse(body) as Record<string, unknown> : {};
    });
    expect(requestBodies).toHaveLength(2);
    expect(requestBodies[0]?.client_message_id).toBe(CLIENT_MESSAGE_ID);
    expect(requestBodies[1]?.client_message_id).toBe(CLIENT_MESSAGE_ID);
  });

  it("marks a partial answer unsaved when the user stops the request", async () => {
    const encoder = new TextEncoder();
    const identity = { turn_id: "turn-06", client_message_id: CLIENT_MESSAGE_ID };
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        Promise.resolve(
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(encoder.encode([
                  frame("message.started", identity),
                  frame("message.delta", { ...identity, text: "Một phần" }),
                ].join("")));
                init?.signal?.addEventListener("abort", () => {
                  controller.error(new DOMException("Stopped", "AbortError"));
                });
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
        ),
      ),
    );
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Gửi mẫu" }));
    expect(await screen.findByText("Một phần")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dừng mẫu" }));

    expect(await screen.findByText("unsaved")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Thử gửi lại" })).toBeEnabled();
  });
});
