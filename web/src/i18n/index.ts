import { en, type MessageKey } from "./en";
import { ru } from "./ru";

export type Locale = "ru" | "en";

const catalogs: Record<Locale, Record<MessageKey, string>> = { ru, en };

export function isLocale(value: unknown): value is Locale {
  return value === "ru" || value === "en";
}

export class I18n {
  private currentLocale: Locale;

  constructor(locale: Locale = "ru") {
    this.currentLocale = locale;
  }

  get locale(): Locale {
    return this.currentLocale;
  }

  setLocale(locale: Locale): void {
    this.currentLocale = locale;
    document.documentElement.lang = locale;
  }

  t(key: MessageKey): string {
    return catalogs[this.currentLocale][key];
  }
}
