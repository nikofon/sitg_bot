const themeVariables: Record<keyof TelegramThemeParams, string> = {
  bg_color: "--tg-theme-bg-color",
  text_color: "--tg-theme-text-color",
  hint_color: "--tg-theme-hint-color",
  link_color: "--tg-theme-link-color",
  button_color: "--tg-theme-button-color",
  button_text_color: "--tg-theme-button-text-color",
  secondary_bg_color: "--tg-theme-secondary-bg-color",
  header_bg_color: "--tg-theme-header-bg-color",
  bottom_bar_bg_color: "--tg-theme-bottom-bar-bg-color",
  accent_text_color: "--tg-theme-accent-text-color",
  section_bg_color: "--tg-theme-section-bg-color",
  section_header_text_color: "--tg-theme-section-header-text-color",
  subtitle_text_color: "--tg-theme-subtitle-text-color",
  destructive_text_color: "--tg-theme-destructive-text-color",
};

export interface MiniAppPlatform {
  readonly initData: string;
  readonly isTelegram: boolean;
  initialize(): void;
  destroy(): void;
  setBackHandler(handler?: () => void): void;
  setMainAction(label: string, handler?: () => void): void;
  notifySuccess(): void;
  notifyError(): void;
  returnToBot(): void;
}

export class TelegramPlatform implements MiniAppPlatform {
  private backHandler?: () => void;
  private mainHandler?: () => void;
  private readonly onBack = (): void => this.backHandler?.();
  private readonly onMain = (): void => this.mainHandler?.();
  private readonly applyEnvironment = (): void => {
    this.applyTheme();
    this.applyViewport();
    this.applyInsets();
  };

  constructor(private readonly webApp?: TelegramWebApp) {}

  get initData(): string {
    return this.webApp?.initData ?? "";
  }

  get isTelegram(): boolean {
    return this.webApp !== undefined;
  }

  initialize(): void {
    if (!this.webApp) return;
    this.applyEnvironment();
    this.webApp.onEvent("themeChanged", this.applyEnvironment);
    this.webApp.onEvent("viewportChanged", this.applyEnvironment);
    this.webApp.onEvent("safeAreaChanged", this.applyEnvironment);
    this.webApp.onEvent("contentSafeAreaChanged", this.applyEnvironment);
    this.webApp.BackButton.onClick(this.onBack);
    this.webApp.MainButton.onClick(this.onMain);
    this.webApp.enableClosingConfirmation?.();
    this.webApp.disableVerticalSwipes?.();
    this.webApp.expand();
    this.webApp.ready();
  }

  destroy(): void {
    if (!this.webApp) return;
    this.webApp.offEvent("themeChanged", this.applyEnvironment);
    this.webApp.offEvent("viewportChanged", this.applyEnvironment);
    this.webApp.offEvent("safeAreaChanged", this.applyEnvironment);
    this.webApp.offEvent("contentSafeAreaChanged", this.applyEnvironment);
    this.webApp.BackButton.offClick(this.onBack);
    this.webApp.MainButton.offClick(this.onMain);
  }

  setBackHandler(handler?: () => void): void {
    this.backHandler = handler;
    if (!this.webApp) return;
    if (handler) this.webApp.BackButton.show();
    else this.webApp.BackButton.hide();
  }

  setMainAction(label: string, handler?: () => void): void {
    this.mainHandler = handler;
    if (!this.webApp) return;
    this.webApp.MainButton.setText(label);
    if (handler) {
      this.webApp.MainButton.show();
      this.webApp.MainButton.enable();
    } else {
      this.webApp.MainButton.hide();
    }
  }

  notifySuccess(): void {
    this.webApp?.HapticFeedback?.notificationOccurred("success");
  }

  notifyError(): void {
    this.webApp?.HapticFeedback?.notificationOccurred("error");
  }

  returnToBot(): void {
    this.webApp?.close();
  }

  private applyTheme(): void {
    if (!this.webApp) return;
    const root = document.documentElement;
    root.dataset.colorScheme = this.webApp.colorScheme;
    for (const [key, variable] of Object.entries(themeVariables) as Array<
      [keyof TelegramThemeParams, string]
    >) {
      const value = this.webApp.themeParams[key];
      if (value) root.style.setProperty(variable, value);
    }
  }

  private applyViewport(): void {
    if (!this.webApp) return;
    const stableHeight = this.webApp.viewportStableHeight || this.webApp.viewportHeight;
    document.documentElement.style.setProperty("--tg-viewport-height", `${stableHeight}px`);
  }

  private applyInsets(): void {
    const safe = this.webApp?.safeAreaInset;
    const content = this.webApp?.contentSafeAreaInset;
    setInsets("safe", safe);
    setInsets("content-safe", content);
  }
}

function setInsets(prefix: string, inset?: TelegramInset): void {
  const root = document.documentElement;
  for (const side of ["top", "right", "bottom", "left"] as const) {
    root.style.setProperty(`--tg-${prefix}-area-inset-${side}`, `${inset?.[side] ?? 0}px`);
  }
}

export function createPlatform(): TelegramPlatform {
  return new TelegramPlatform(window.Telegram?.WebApp);
}
