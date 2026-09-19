import { ApiError } from "../api/http";
import { SseParser, streamConversationMessage, type ProductChatEvent } from "./chatApi";

function streamResponse(chunks: string[], contentType = "text/event-stream; charset=utf-8") {
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
    { status: 200, headers: { "Content-Type": contentType } },
  );
}

function frame(name: string, payload: Record<string, unknown>): string {
  return `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
}

beforeEach(() => {
  document.cookie = "kira_csrf_dev=stream-token; Path=/";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SSE parser", () => {
  it("preserves split CRLF frames and joins multiple data lines", () => {
    const parser = new SseParser();

    expect(parser.push("event: message.del")).toEqual([]);
    expect(parser.push("ta\r\ndata: {\"text\":\"xin\"}\r")).toEqual([]);
    expect(parser.push("\n\r\n:event keepalive\r\n\r\n")).toEqual([
      { name: "message.delta", data: "{\"text\":\"xin\"}" },
    ]);
    expect(parser.finish("event: note\ndata: first\ndata: second")).toEqual([
      { name: "note", data: "first\nsecond" },
    ]);
  });
});

describe("product chat stream", () => {
  it("streams split events with credentials and reports persisted completion", async () => {
    const clientMessageId = "11111111-1111-4111-8111-111111111111";
    const identity = { turn_id: "turn-01", client_message_id: clientMessageId };
    const body = [
      frame("message.started", identity),
      frame("message.delta", { ...identity, text: "Xin " }),
      frame("message.delta", { ...identity, text: "chào" }),
      frame("message.completed", { ...identity, replayed: false, event_id: "event-01" }),
    ].join("");
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input === "" || init?.method !== "POST") {
        return Promise.reject(new Error("Unexpected chat request"));
      }
      return Promise.resolve(
        streamResponse([body.slice(0, 17), body.slice(17, 61), body.slice(61)]),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const events: ProductChatEvent[] = [];

    const completed = await streamConversationMessage({
      sessionId: "session/one",
      clientMessageId,
      message: "hello",
      signal: new AbortController().signal,
      onEvent: (event) => events.push(event),
    });

    expect(events.map((event) => event.type)).toEqual([
      "message.started",
      "message.delta",
      "message.delta",
      "message.completed",
    ]);
    expect(completed).toMatchObject({ replayed: false, event_id: "event-01" });
    const [requestUrl, requestInit] = fetchMock.mock.calls[0] ?? [];
    expect(requestUrl).toBe("/api/v1/conversations/session%2Fone/messages");
    expect(requestInit).toMatchObject({ method: "POST", credentials: "include" });
    expect((requestInit?.headers as Record<string, string>)["X-CSRF-Token"]).toBe(
      "stream-token",
    );
    expect(typeof requestInit?.body).toBe("string");
    const requestBody = typeof requestInit?.body === "string" ? requestInit.body : "{}";
    expect(JSON.parse(requestBody)).toEqual({
      client_message_id: clientMessageId,
      message: "hello",
    });
  });

  it("accepts a completed replay without changing the client message ID", async () => {
    const clientMessageId = "22222222-2222-4222-8222-222222222222";
    const identity = { turn_id: "turn-02", client_message_id: clientMessageId };
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          streamResponse([
            frame("message.started", identity),
            frame("message.delta", { ...identity, text: "Câu trả lời đã lưu" }),
            frame("message.completed", { ...identity, replayed: true }),
          ]),
        ),
      ),
    );

    const completed = await streamConversationMessage({
      sessionId: "session-two",
      clientMessageId,
      message: "retry",
      signal: new AbortController().signal,
      onEvent: () => undefined,
    });

    expect(completed.replayed).toBe(true);
    expect(completed.client_message_id).toBe(clientMessageId);
  });

  it("fails closed when EOF arrives before durable completion", async () => {
    const clientMessageId = "33333333-3333-4333-8333-333333333333";
    const identity = { turn_id: "turn-03", client_message_id: clientMessageId };
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          streamResponse([
            frame("message.started", identity),
            frame("message.delta", { ...identity, text: "Chưa lưu" }),
          ]),
        ),
      ),
    );

    await expect(
      streamConversationMessage({
        sessionId: "session-three",
        clientMessageId,
        message: "hello",
        signal: new AbortController().signal,
        onEvent: () => undefined,
      }),
    ).rejects.toMatchObject({ code: "CHAT_STREAM_INTERRUPTED", retryable: true });
  });

  it("turns a failed event into a sanitized retryable API error", async () => {
    const clientMessageId = "44444444-4444-4444-8444-444444444444";
    const identity = { turn_id: "turn-04", client_message_id: clientMessageId };
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          streamResponse([
            frame("message.started", identity),
            frame("message.failed", {
              ...identity,
              code: "KIRA_TIMEOUT",
              message: "provider private detail",
              retryable: true,
              correlation_id: "corr-stream",
            }),
          ]),
        ),
      ),
    );

    const caught = await streamConversationMessage({
      sessionId: "session-four",
      clientMessageId,
      message: "hello",
      signal: new AbortController().signal,
      onEvent: () => undefined,
    }).catch((error: unknown) => error);

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).toMatchObject({
      code: "KIRA_TIMEOUT",
      correlationId: "corr-stream",
      retryable: true,
    });
    expect((caught as ApiError).message).not.toContain("provider private detail");
  });
});
