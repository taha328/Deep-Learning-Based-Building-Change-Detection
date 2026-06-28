export type Theme = "light" | "dark";

export function resolveInitialTheme(storedTheme: string | null): Theme {
  if (storedTheme === "light" || storedTheme === "dark") {
    return storedTheme;
  }
  return "light";
}
