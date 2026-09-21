(() => {
  const storageKey = "kira-theme";
  let saved = null;
  try {
    saved = localStorage.getItem(storageKey);
  } catch {
    // Storage can be unavailable in hardened/private browser contexts.
  }
  const preference = saved === "light" || saved === "dark" || saved === "system"
    ? saved
    : "system";
  const resolved = preference === "system"
    ? (typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light")
    : preference;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themePreference = preference;
  document.documentElement.style.colorScheme = resolved;
})();
