import { apiRequest } from "../api/http";

export interface AuthenticatedUser {
  user_id: string;
  username: string;
}

export interface LoginCredentials {
  username: string;
  password: string;
}

export interface PasswordChange {
  current_password: string;
  new_password: string;
}

export function getCurrentUser(signal?: AbortSignal): Promise<AuthenticatedUser> {
  return apiRequest<AuthenticatedUser>(
    "/api/v1/auth/me",
    signal === undefined ? {} : { signal },
  );
}

export function login(credentials: LoginCredentials): Promise<AuthenticatedUser> {
  return apiRequest<AuthenticatedUser>("/api/v1/auth/login", {
    method: "POST",
    body: credentials,
  });
}

export async function logout(): Promise<void> {
  await apiRequest<undefined>("/api/v1/auth/logout", {
    method: "POST",
    csrf: true,
  });
}

export async function changePassword(change: PasswordChange): Promise<void> {
  await apiRequest<undefined>("/api/v1/auth/change-password", {
    method: "POST",
    body: change,
    csrf: true,
  });
}
