import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/app/ThemeContext";
import { useI18n } from "@/lib/i18n";

export function ThemeSwitcher() {
  const { theme, toggleTheme } = useTheme();
  const { t } = useI18n();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      title={theme === "light" ? t("theme.dark_mode") : t("theme.light_mode")}
      className="flex h-11 w-11 items-center justify-center rounded border border-sidebar-border bg-sidebar text-sidebar-foreground transition-colors hover:bg-surface"
      aria-label={theme === "light" ? t("theme.dark_mode") : t("theme.light_mode")}
    >
      {theme === "light" ? (
        <Moon className="h-4 w-4" />
      ) : (
        <Sun className="h-4 w-4" />
      )}
    </button>
  );
}
