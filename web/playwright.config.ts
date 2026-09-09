import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  use: { baseURL: "http://127.0.0.1:5179", trace: "retain-on-failure" },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5179 --strictPort",
    url: "http://127.0.0.1:5179",
    reuseExistingServer: !process.env.CI,
  },
});
