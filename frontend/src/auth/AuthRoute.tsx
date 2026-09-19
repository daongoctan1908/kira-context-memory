import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { AuthStatusPage } from "../components/AuthStatusPage";
import { useAuth } from "./useAuth";

export function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const location = useLocation();

  if (auth.status === "loading") {
    return <AuthStatusPage mode="loading" />;
  }
  if (auth.status === "unavailable") {
    return <AuthStatusPage mode="unavailable" onRetry={() => void auth.refresh()} />;
  }
  if (auth.status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return children;
}

export function AnonymousOnly({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const location = useLocation();

  if (auth.status === "loading") {
    return <AuthStatusPage mode="loading" />;
  }
  if (auth.status === "unavailable") {
    return <AuthStatusPage mode="unavailable" onRetry={() => void auth.refresh()} />;
  }
  if (auth.status === "authenticated") {
    const state = location.state as { from?: unknown } | null;
    const from = typeof state?.from === "string" ? state.from : undefined;
    const destination = from?.startsWith("/") === true && !from.startsWith("//")
      ? from
      : "/chat/new";
    return <Navigate to={destination} replace />;
  }
  return children;
}
