import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, AUTH_SESSION_INVALID_EVENT } from "../api/http";
import {
  changePassword as requestPasswordChange,
  getCurrentUser,
  login as requestLogin,
  logout as requestLogout,
  type AuthenticatedUser,
  type LoginCredentials,
  type PasswordChange,
} from "./authApi";
import {
  AuthContext,
  type AuthContextValue,
  type AuthNotice,
  type AuthStatus,
} from "./AuthContext";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<AuthenticatedUser | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<AuthNotice>(null);

  const becomeAnonymous = useCallback(() => {
    setUser(null);
    setError(null);
    setStatus("anonymous");
  }, []);

  const refresh = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const currentUser = await getCurrentUser();
      setUser(currentUser);
      setStatus("authenticated");
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "AUTH_SESSION_INVALID") {
        becomeAnonymous();
        return;
      }
      setUser(null);
      setError(normalizeApiError(caught));
      setStatus("unavailable");
    }
  }, [becomeAnonymous]);

  useEffect(() => {
    const controller = new AbortController();
    const bootstrap = async () => {
      try {
        const currentUser = await getCurrentUser(controller.signal);
        setUser(currentUser);
        setError(null);
        setStatus("authenticated");
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === "AbortError") {
          return;
        }
        if (caught instanceof ApiError && caught.code === "AUTH_SESSION_INVALID") {
          becomeAnonymous();
          return;
        }
        setUser(null);
        setError(normalizeApiError(caught));
        setStatus("unavailable");
      }
    };
    void bootstrap();
    return () => {
      controller.abort();
    };
  }, [becomeAnonymous]);

  useEffect(() => {
    window.addEventListener(AUTH_SESSION_INVALID_EVENT, becomeAnonymous);
    return () => {
      window.removeEventListener(AUTH_SESSION_INVALID_EVENT, becomeAnonymous);
    };
  }, [becomeAnonymous]);

  const login = useCallback(async (credentials: LoginCredentials) => {
    const authenticatedUser = await requestLogin(credentials);
    setNotice(null);
    setUser(authenticatedUser);
    setError(null);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    try {
      await requestLogout();
      setNotice("signedOut");
      becomeAnonymous();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "AUTH_SESSION_INVALID") {
        becomeAnonymous();
        return;
      }
      throw caught;
    }
  }, [becomeAnonymous]);

  const changePassword = useCallback(
    async (change: PasswordChange) => {
      await requestPasswordChange(change);
      setNotice("passwordChanged");
      becomeAnonymous();
    },
    [becomeAnonymous],
  );

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, error, notice, login, logout, changePassword, refresh }),
    [status, user, error, notice, login, logout, changePassword, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

function normalizeApiError(error: unknown): ApiError {
  return error instanceof ApiError
    ? error
    : new ApiError(0, "AUTH_UNAVAILABLE", "Authentication is unavailable", {
        retryable: true,
        cause: error,
      });
}
