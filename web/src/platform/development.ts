export interface IdentitySource {
  initData: string;
  developmentHarness: boolean;
}

export function resolveIdentity(telegramInitData: string): IdentitySource {
  if (telegramInitData) return { initData: telegramInitData, developmentHarness: false };
  const localHost = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
  const localInitData = import.meta.env.DEV && localHost ? import.meta.env.VITE_DEV_INIT_DATA : undefined;
  return { initData: localInitData ?? "", developmentHarness: Boolean(localInitData) };
}
