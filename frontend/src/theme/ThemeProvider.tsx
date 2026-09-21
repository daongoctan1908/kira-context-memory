import { useEffect, useMemo, useState, type ReactNode } from "react";

import {
  ThemeContext,
  type ResolvedTheme,
  type ThemePreference,
} from "./ThemeContext";

export const THEME_STORAGE_KEY = "kira-theme";

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreference] = useState<ThemePreference>(readInitialPreference);
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(readSystemTheme);
  const resolved = preference === "system" ? systemTheme : preference;

  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return undefined;
    }
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => {
      setSystemTheme(media.matches ? "dark" : "light");
    };
    media.addEventListener("change", update);
    return () => {
      media.removeEventListener("change", update);
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.style.colorScheme = resolved;
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  }, [preference, resolved]);

  const value = useMemo(
    () => ({ preference, resolved, setPreference }),
    [preference, resolved],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

function readInitialPreference(): ThemePreference {
  const bootstrapValue = document.documentElement.dataset.themePreference;
  if (isThemePreference(bootstrapValue)) {
    return bootstrapValue;
  }
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  return isThemePreference(stored) ? stored : "system";
}

function readSystemTheme(): ResolvedTheme {
  return typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}
