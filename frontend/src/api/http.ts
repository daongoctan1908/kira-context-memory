export interface GatewayErrorPayload {
  code: string;
  message: string;
  correlation_id: string;
  retryable: boolean;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly retryable: boolean;

  constructor(
    status: number,
    code: string,
    message: string,
    options: {
      correlationId?: string | null;
      retryable?: boolean;
      cause?: unknown;
    } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = options.correlationId ?? null;
    this.retryable = options.retryable ?? false;
  }
}

interface ApiRequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  csrf?: boolean;
}

export async function apiRequest<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { body, csrf = false, headers: suppliedHeaders, ...requestOptions } = options;
  const headers = new Headers(suppliedHeaders);
  headers.set("Accept", "application/json");
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (csrf) {
    const csrfToken = readCsrfToken();
    if (csrfToken === null) {
      throw new ApiError(403, "AUTH_CSRF_MISSING", "CSRF token is unavailable");
    }
    headers.set("X-CSRF-Token", csrfToken);
  }

  let response: Response;
  try {
    const request: RequestInit = {
      ...requestOptions,
      credentials: "include",
      headers,
    };
    if (body !== undefined) {
      request.body = JSON.stringify(body);
    }
    response = await fetch(path, request);
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
    const payload = await readGatewayError(response);
    if (payload.code === "AUTH_SESSION_INVALID") {
      window.dispatchEvent(new Event(AUTH_SESSION_INVALID_EVENT));
    }
    throw new ApiError(response.status, payload.code, payload.message, {
      correlationId: payload.correlation_id,
      retryable: payload.retryable,
    });
  }
  if (response.status === 204) {
    return undefined as T;
  }
  try {
    return (await response.json()) as T;
  } catch (error) {
    throw new ApiError(response.status, "API_RESPONSE_INVALID", "Invalid API response", {
      cause: error,
    });
  }
}

export const AUTH_SESSION_INVALID_EVENT = "kira:auth-session-invalid";

export function readCsrfToken(cookieSource: string = document.cookie): string | null {
  const supportedNames = new Set(["__Host-kira_csrf", "kira_csrf_dev"]);
  for (const part of cookieSource.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0) {
      continue;
    }
    const name = part.slice(0, separator).trim();
    if (supportedNames.has(name)) {
      try {
        return decodeURIComponent(part.slice(separator + 1));
      } catch {
        return null;
      }
    }
  }
  return null;
}

async function readGatewayError(response: Response): Promise<GatewayErrorPayload> {
  try {
    const candidate: unknown = await response.json();
    if (isGatewayError(candidate)) {
      return candidate;
    }
  } catch {
    // The public fallback deliberately excludes response bodies.
  }
  return {
    code: "API_REQUEST_FAILED",
    message: "Request failed",
    correlation_id: response.headers.get("X-Correlation-ID") ?? "",
    retryable: response.status >= 500,
  };
}

function isGatewayError(value: unknown): value is GatewayErrorPayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.code === "string" &&
    typeof candidate.message === "string" &&
    typeof candidate.correlation_id === "string" &&
    typeof candidate.retryable === "boolean"
  );
}
