import { describe, expect, it, vi } from "vitest";
import { TelegramPlatform } from "./telegram";

describe("Telegram haptic feedback", () => {
  it("uses Telegram's supported notification feedback", () => {
    const notificationOccurred = vi.fn();
    const platform = new TelegramPlatform({ HapticFeedback: { notificationOccurred } } as unknown as TelegramWebApp);
    platform.notifySuccess();
    platform.notifyError();
    expect(notificationOccurred.mock.calls).toEqual([["success"], ["error"]]);
  });

  it("is optional outside Telegram and on unsupported clients", () => {
    for (const webApp of [undefined, {} as TelegramWebApp,
      { HapticFeedback: { notificationOccurred: () => { throw new Error("unsupported"); } } } as unknown as TelegramWebApp]) {
      const platform = new TelegramPlatform(webApp);
      expect(() => platform.notifySuccess()).not.toThrow();
      expect(() => platform.notifyError()).not.toThrow();
    }
  });
});
