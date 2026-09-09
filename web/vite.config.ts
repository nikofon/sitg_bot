import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "SITG_");
  return {
    build: {
      sourcemap: false,
      target: "es2022",
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": env.SITG_MINIAPP_API_TARGET ?? "http://127.0.0.1:8080",
      },
    },
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.ts"],
      restoreMocks: true,
    },
  };
});
