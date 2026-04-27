import { useI18n } from "@/lib/i18n";

export function LanguageSwitcher() {
  const { language, setLanguage, t } = useI18n();

  const toggleLanguage = () => {
    setLanguage(language === "fr" ? "en" : "fr");
  };

  return (
    <button
      type="button"
      onClick={toggleLanguage}
      title={t("language.select")}
      className="flex h-11 w-11 items-center justify-center rounded border border-sidebar-border bg-sidebar text-xs font-semibold text-sidebar-foreground transition-colors hover:bg-surface"
    >
      {language === "fr" ? "FR" : "EN"}
    </button>
  );
}
