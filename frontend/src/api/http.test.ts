import { apiRequest, ApiError, readCsrfToken } from "./http";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API client security contract", () => {
  it("reads only supported CSRF cookies and decodes their value", () => {
    expect(readCsrfToken("other=x; kira_csrf_dev=token%2Bvalue")).toBe("token+value");
    expect(readCsrfToken("__Host-kira_csrf=secure-token")).toBe("secure-token");
    expect(readCsrfToken("session=private")).toBeNull();
    expect(readCsrfToken("kira_csrf_dev=%E0%A4%A")).toBeNull();
  });

  it("fails before an unsafe request when the readable CSRF cookie is absent", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      apiRequest("/api/v1/auth/logout", { method: "POST", csrf: true }),
    ).rejects.toMatchObject({ code: "AUTH_CSRF_MISSING" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps malformed backend failures without exposing the response body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response("private database detail", {
            status: 503,
            headers: { "X-Correlation-ID": "corr-safe" },
          }),
        ),
      ),
    );

    const caught = await apiRequest("/api/v1/auth/me").catch((error: unknown) => error);
    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).toMatchObject({
      code: "API_REQUEST_FAILED",
      correlationId: "corr-safe",
      retryable: true,
    });
    expect((caught as ApiError).message).not.toContain("private database detail");
  });

  it("announces a revoked or expired session without exposing its body", async () => {
    const sessionInvalid = vi.fn();
    window.addEventListener("kira:auth-session-invalid", sessionInvalid);
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              code: "AUTH_SESSION_INVALID",
              message: "Authentication required",
              correlation_id: "corr-expired",
              retryable: false,
            }),
            { status: 401, headers: { "Content-Type": "application/json" } },
          ),
        ),
      ),
    );

    await expect(apiRequest("/api/v1/auth/me")).rejects.toMatchObject({
      code: "AUTH_SESSION_INVALID",
    });
    expect(sessionInvalid).toHaveBeenCalledOnce();
    window.removeEventListener("kira:auth-session-invalid", sessionInvalid);
  });
});
