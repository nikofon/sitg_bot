import "./styles.css";

import { ApiClient } from "./api/client";
import { MiniAppShell } from "./app";
import { resolveIdentity } from "./platform/development";
import { createPlatform } from "./platform/telegram";
import { Router } from "./routing/router";

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Mini App root element is missing");

const platform = createPlatform();
const identity = resolveIdentity(platform.initData);
const shell = new MiniAppShell(
  root,
  new ApiClient(identity.initData),
  new Router(),
  platform,
  identity.developmentHarness,
);

shell.start();
window.addEventListener("pagehide", () => shell.stop(), { once: true });
