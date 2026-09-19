import { createContext } from "react";

import type { ApiError } from "../api/http";
import type { AuthenticatedUser, LoginCredentials, PasswordChange } from "./authApi";

export type AuthStatus = "loading" | "authenticated" | "anonymous" | "unavailable";
export type AuthNotice = "passwordChanged" | "signedOut" | null;

export interface AuthContextValue {
  status: AuthStatus;
  user: AuthenticatedUser | null;
  error: ApiError | null;
  notice: AuthNotice;
  login: (credentials: LoginCredentials) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (change: PasswordChange) => Promise<void>;
  refresh: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);
