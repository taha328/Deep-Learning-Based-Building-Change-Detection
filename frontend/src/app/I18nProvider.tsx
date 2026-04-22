import React, { useState, useEffect } from "react";
import { I18nContext, type Language, type I18nContextType, getStoredLanguage, setStoredLanguage } from "@/lib/i18n";
import { getTranslation, type TranslationKey } from "@/lib/translations";

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguageState] = useState<Language>("fr");
  const [isLoaded, setIsLoaded] = useState(false);

  // Initialize language from localStorage on mount
  useEffect(() => {
    const storedLanguage = getStoredLanguage();
    setLanguageState(storedLanguage);
    setIsLoaded(true);
  }, []);

  const setLanguage = (lang: Language) => {
    setLanguageState(lang);
    setStoredLanguage(lang);
  };

  const t = (key: string, defaultValue?: string): string => {
    return getTranslation(language, key as TranslationKey, defaultValue ?? key);
  };

  const value: I18nContextType = {
    language,
    setLanguage,
    t,
  };

  // Don't render children until language is initialized to avoid flashing
  if (!isLoaded) {
    return null;
  }

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
