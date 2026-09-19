import { apiErrorFromResponse, ApiError, readCsrfToken } from "../api/http";

interface EventBase {
  turn_id: string;
  client_message_id: string;
}

export interface MessageStartedEvent extends EventBase {
  type: "message.started";
}

export interface MessageDeltaEvent extends EventBase {
  type: "message.delta";
  text: string;
}

export interface MessageCompletedEvent extends EventBase {
  type: "message.completed";
  replayed: boolean;
  event_id?: string;
}

export interface MessageFailedEvent extends EventBase {
  type: "message.failed";
  code: string;
  message: string;
  retryable: boolean;
  correlation_id: string;
}

export type ProductChatEvent =
  | MessageStartedEvent
  | MessageDeltaEvent
  | MessageCompletedEvent
  | MessageFailedEvent;

interface ParsedSseEvent {
  name: string;
  data: string;
}

export async function streamConversationMessage({
  sessionId,
  clientMessageId,
  message,
  signal,
  onEvent,
}: {
  sessionId: string;
  clientMessageId: string;
  message: string;
  signal: AbortSignal;
  onEvent: (event: ProductChatEvent) => void;
}): Promise<MessageCompletedEvent> {
  const csrfToken = readCsrfToken();
  if (csrfToken === null) {
    throw new ApiError(403, "AUTH_CSRF_MISSING", "CSRF token is unavailable");
  }

  let response: Response;
  try {
    response = await fetch(
      `/api/v1/conversations/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        credentials: "include",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ client_message_id: clientMessageId, message }),
        signal,
      },
    );
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ApiError(0, "NETWORK_ERROR", "Network request failed", {
      retryable: true,
      cause: error,
    });
  }

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }
  if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("text/event-stream")) {
    throw protocolError("Chat endpoint did not return an event stream");
  }
  if (response.body === null) {
    throw protocolError("Chat response stream is unavailable");
  }

  const parser = new SseParser();
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let state: StreamState = { started: false, completed: null };
  try {
    let chunk = await reader.read();
    while (!chunk.done) {
      const parsed = parser.push(decoder.decode(chunk.value, { stream: true }));
      state = processEvents(parsed, clientMessageId, onEvent, state);
      chunk = await reader.read();
    }
    const trailing = parser.finish(decoder.decode());
    state = processEvents(trailing, clientMessageId, onEvent, state);
  } finally {
    reader.releaseLock();
  }

  if (state.completed === null) {
    throw new ApiError(0, "CHAT_STREAM_INTERRUPTED", "Chat stream ended before completion", {
      retryable: true,
    });
  }
  return state.completed;
}

interface StreamState {
  started: boolean;
  completed: MessageCompletedEvent | null;
}

function processEvents(
  parsed: ParsedSseEvent[],
  clientMessageId: string,
  onEvent: (event: ProductChatEvent) => void,
  current: StreamState,
): StreamState {
  let state = current;
  for (const item of parsed) {
    const event = parseProductEvent(item);
    if (event === null) {
      continue;
    }
    if (event.client_message_id !== clientMessageId) {
      throw protocolError("Chat event client message ID does not match the request");
    }
    if (state.completed !== null) {
      throw protocolError("Chat stream emitted data after completion");
    }
    if (event.type === "message.started") {
      if (state.started) {
        throw protocolError("Chat stream emitted more than one start event");
      }
      state = { ...state, started: true };
    } else if (!state.started) {
      throw protocolError("Chat stream emitted data before its start event");
    }
    onEvent(event);
    if (event.type === "message.failed") {
      throw new ApiError(0, event.code, "Chat request failed", {
        correlationId: event.correlation_id,
        retryable: event.retryable,
      });
    }
    if (event.type === "message.completed") {
      state = { ...state, completed: event };
    }
  }
  return state;
}

export class SseParser {
  private buffer = "";

  push(chunk: string): ParsedSseEvent[] {
    this.buffer += chunk;
    const events: ParsedSseEvent[] = [];
    let boundary = /\r?\n\r?\n/.exec(this.buffer);
    while (boundary !== null) {
      const frame = this.buffer.slice(0, boundary.index);
      this.buffer = this.buffer.slice(boundary.index + boundary[0].length);
      const parsed = parseFrame(frame);
      if (parsed !== null) {
        events.push(parsed);
      }
      boundary = /\r?\n\r?\n/.exec(this.buffer);
    }
    return events;
  }

  finish(trailing = ""): ParsedSseEvent[] {
    const events = this.push(trailing);
    if (this.buffer.trim().length > 0) {
      const parsed = parseFrame(this.buffer);
      if (parsed !== null) {
        events.push(parsed);
      }
    }
    this.buffer = "";
    return events;
  }
}

function parseFrame(frame: string): ParsedSseEvent | null {
  let name = "message";
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) {
      continue;
    }
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const rawValue = separator < 0 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "event") {
      name = value;
    } else if (field === "data") {
      data.push(value);
    }
  }
  return data.length === 0 ? null : { name, data: data.join("\n") };
}

function parseProductEvent(item: ParsedSseEvent): ProductChatEvent | null {
  if (!SUPPORTED_EVENTS.has(item.name)) {
    return null;
  }
  let payload: unknown;
  try {
    payload = JSON.parse(item.data) as unknown;
  } catch (error) {
    throw protocolError("Chat event data is not valid JSON", error);
  }
  if (!isRecord(payload)) {
    throw protocolError("Chat event is missing its identity fields");
  }
  const turnId = readString(payload, "turn_id");
  const clientMessageId = readString(payload, "client_message_id");
  if (turnId === null || clientMessageId === null) {
    throw protocolError("Chat event is missing its identity fields");
  }
  const base = {
    turn_id: turnId,
    client_message_id: clientMessageId,
  };
  if (item.name === "message.started") {
    return { type: item.name, ...base };
  }
  if (item.name === "message.delta") {
    const text = readString(payload, "text");
    if (text === null) {
      throw protocolError("Chat delta is missing text");
    }
    return { type: item.name, ...base, text };
  }
  if (item.name === "message.completed") {
    if (typeof payload.replayed !== "boolean") {
      throw protocolError("Chat completion is missing replay state");
    }
    const event: MessageCompletedEvent = {
      type: item.name,
      ...base,
      replayed: payload.replayed,
    };
    const eventId = readString(payload, "event_id");
    if (eventId !== null) {
      event.event_id = eventId;
    }
    return event;
  }
  if (
    readString(payload, "code") === null
    || readString(payload, "message") === null
    || typeof payload.retryable !== "boolean"
    || readString(payload, "correlation_id") === null
  ) {
    throw protocolError("Chat failure is missing required fields");
  }
  return {
    type: "message.failed",
    ...base,
    code: readString(payload, "code") ?? "CHAT_FAILED",
    message: readString(payload, "message") ?? "Chat request failed",
    retryable: payload.retryable,
    correlation_id: readString(payload, "correlation_id") ?? "unknown",
  };
}

const SUPPORTED_EVENTS = new Set([
  "message.started",
  "message.delta",
  "message.completed",
  "message.failed",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function readString(value: Record<string, unknown>, key: string): string | null {
  const item = value[key];
  return typeof item === "string" ? item : null;
}

function protocolError(message: string, cause?: unknown): ApiError {
  return cause === undefined
    ? new ApiError(0, "CHAT_STREAM_INVALID", message, { retryable: true })
    : new ApiError(0, "CHAT_STREAM_INVALID", message, { retryable: true, cause });
}
