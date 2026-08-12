import { defineConfig } from "@playwright/test";

const baseURL = process.env.LLMOLYMPIC_WEB_E2E_URL;
if (!baseURL) throw new Error("LLMOLYMPIC_WEB_E2E_URL is required");

const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;

export default defineConfig({
  testDir: ".",
  testMatch: "observer.spec.mjs",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: "line",
  outputDir: "../../test-results/playwright",
  use: {
    baseURL,
    browserName: "chromium",
    colorScheme: "light",
    reducedMotion: "reduce",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
    viewport: { width: 1280, height: 900 },
    launchOptions: executablePath ? { executablePath } : {},
  },
});
