interface TelegramThemeParams {
  bg_color?: string;
  text_color?: string;
  hint_color?: string;
  link_color?: string;
  button_color?: string;
  button_text_color?: string;
  secondary_bg_color?: string;
  header_bg_color?: string;
  bottom_bar_bg_color?: string;
  accent_text_color?: string;
  section_bg_color?: string;
  section_header_text_color?: string;
  subtitle_text_color?: string;
  destructive_text_color?: string;
}

interface TelegramInset {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

interface TelegramButton {
  isVisible: boolean;
  show(): TelegramButton;
  hide(): TelegramButton;
  onClick(callback: () => void): TelegramButton;
  offClick(callback: () => void): TelegramButton;
}

interface TelegramMainButton extends TelegramButton {
  setText(text: string): TelegramMainButton;
  enable(): TelegramMainButton;
  disable(): TelegramMainButton;
  showProgress(leaveActive?: boolean): TelegramMainButton;
  hideProgress(): TelegramMainButton;
}

interface TelegramWebApp {
  initData: string;
  colorScheme: "light" | "dark";
  themeParams: TelegramThemeParams;
  viewportHeight: number;
  viewportStableHeight: number;
  isExpanded: boolean;
  safeAreaInset?: TelegramInset;
  contentSafeAreaInset?: TelegramInset;
  BackButton: TelegramButton;
  MainButton: TelegramMainButton;
  HapticFeedback?: {
    impactOccurred(style: "light" | "medium" | "heavy" | "rigid" | "soft"): void;
    notificationOccurred(type: "error" | "success" | "warning"): void;
  };
  ready(): void;
  expand(): void;
  close(): void;
  sendData(data: string): void;
  enableClosingConfirmation?(): void;
  disableVerticalSwipes?(): void;
  onEvent(eventType: string, callback: () => void): void;
  offEvent(eventType: string, callback: () => void): void;
}

interface Window {
  Telegram?: { WebApp?: TelegramWebApp };
}
