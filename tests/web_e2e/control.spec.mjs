import { spawn } from "node:child_process";
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { createServer as createHttpServer } from "node:http";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, test as base } from "@playwright/test";

const ADMIN_STORAGE_KEY = "llmolympic.control.admin";
const URL_SAFE_CAPABILITY = /^[A-Za-z0-9_-]{43}$/;
const PARTICIPATION_PATH = /^\/participate\/([A-Za-z0-9._:-]+)\/([A-Za-z0-9._:-]+)$/;
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const CONTROL_START_TIMEOUT_MS = 20_000;
const CONTROL_TEST_TIMEOUT_MS = 60_000;
const FAKE_PROVIDER_MAX_BODY_BYTES = 64 * 1024;

// Admin, seat, and ephemeral Provider credentials are real secrets. Disabling
// screenshots and traces keeps password fields, Authorization headers,
// sessionStorage, and DOM link attributes out of retained Playwright artifacts.
// No test navigates to a secret URL.
base.use({ screenshot: "off", trace: "off", video: "off" });

function summarizedViolations(results) {
  return results.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    targets: violation.nodes.map((node) => node.target),
  }));
}

async function expectNoWcagViolations(page) {
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  expect(summarizedViolations(results), "axe WCAG A/AA violations").toEqual([]);
}

function redactSensitiveText(value, secrets = []) {
  let safe = String(value)
    .replace(/#(?:admin|capability)=[A-Za-z0-9_-]+/gu, "#credential=<redacted>")
    .replace(/Bearer\s+[A-Za-z0-9_-]+/giu, "Bearer <redacted>");
  for (const secret of secrets) {
    if (secret) safe = safe.split(secret).join("<redacted>");
  }
  return safe.slice(-2_000);
}

function monitorBrowserMessages(page, monitor, secrets) {
  const record = (label, value, { error = false } = {}) => {
    const raw = String(value);
    if (secrets.some((secret) => secret && raw.includes(secret))) {
      monitor.sensitive = true;
    }
    const safe = `${label}: ${redactSensitiveText(raw, secrets)}`;
    monitor.messages.push(safe);
    if (error) monitor.errors.push(safe);
  };
  page.on("console", (message) => {
    record(`console:${message.type()}`, message.text(), {
      error: message.type() === "error",
    });
  });
  page.on("pageerror", (error) => {
    record("pageerror", error.message, { error: true });
  });
}

async function reserveLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = createNetServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close(() => reject(new Error("could not reserve a loopback port")));
        return;
      }
      server.close((error) => {
        if (error) reject(error);
        else resolve(address.port);
      });
    });
  });
}

function digestAuthorization(value) {
  return createHash("sha256").update(value, "utf8").digest();
}

function sendFakeProviderJSON(response, status, payload) {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(status, {
    "connection": "close",
    "content-type": "application/json; charset=utf-8",
  });
  response.end(JSON.stringify(payload));
}

async function startFakeOpenAI(expectedCredential) {
  const expectedAuthorizationDigest = digestAuthorization(
    `Bearer ${expectedCredential}`,
  );
  const summary = {
    authorizationMatched: true,
    bodyCredentialFree: true,
    bodyWithinLimit: true,
    contentTypeHeaderMatched: true,
    jsonMatched: true,
    maxTokensMatched: true,
    messageContentTypesMatched: true,
    methodMatched: true,
    modelMatched: true,
    pathMatched: true,
    promptMatched: true,
    requestCount: 0,
    rolesMatched: true,
    transportMatched: true,
  };
  const server = createHttpServer((request, response) => {
    summary.requestCount += 1;
    const receivedDigest = digestAuthorization(
      typeof request.headers.authorization === "string"
        ? request.headers.authorization
        : "",
    );
    const authorizationMatched = timingSafeEqual(
      expectedAuthorizationDigest,
      receivedDigest,
    );
    const contentTypeHeader = request.headers["content-type"];
    const contentTypeHeaderMatched = typeof contentTypeHeader === "string"
      && contentTypeHeader.split(";", 1)[0].trim().toLowerCase() === "application/json";
    const methodMatched = request.method === "POST";
    const pathMatched = request.url === "/v1/chat/completions";
    summary.authorizationMatched = summary.authorizationMatched && authorizationMatched;
    summary.contentTypeHeaderMatched = summary.contentTypeHeaderMatched
      && contentTypeHeaderMatched;
    summary.methodMatched = summary.methodMatched && methodMatched;
    summary.pathMatched = summary.pathMatched && pathMatched;

    let bodyBytes = 0;
    let chunks = [];
    request.on("data", (chunk) => {
      bodyBytes += chunk.length;
      if (bodyBytes > FAKE_PROVIDER_MAX_BODY_BYTES) {
        summary.bodyWithinLimit = false;
        chunks = [];
        return;
      }
      chunks.push(chunk);
    });
    request.on("error", () => {
      summary.transportMatched = false;
      sendFakeProviderJSON(response, 400, {
        error: { message: "request rejected", type: "invalid_request_error" },
      });
    });
    request.on("end", () => {
      if (bodyBytes > FAKE_PROVIDER_MAX_BODY_BYTES) {
        sendFakeProviderJSON(response, 413, {
          error: { message: "request rejected", type: "invalid_request_error" },
        });
        return;
      }

      const rawBody = Buffer.concat(chunks, bodyBytes);
      summary.bodyCredentialFree = summary.bodyCredentialFree
        && !rawBody.includes(Buffer.from(expectedCredential, "utf8"));
      let body;
      try {
        body = JSON.parse(rawBody.toString("utf8"));
      } catch (_error) {
        summary.jsonMatched = false;
        sendFakeProviderJSON(response, 400, {
          error: { message: "request rejected", type: "invalid_request_error" },
        });
        return;
      } finally {
        chunks = [];
      }

      const messages = body && typeof body === "object" && Array.isArray(body.messages)
        ? body.messages
        : [];
      const modelMatched = body && typeof body === "object" && body.model === "fake-model";
      const rolesMatched = messages.length === 2
        && messages[0] && messages[0].role === "system"
        && messages[1] && messages[1].role === "user";
      const messageContentTypesMatched = messages.length === 2
        && messages.every((message) => (
          message && typeof message === "object" && typeof message.content === "string"
        ));
      const promptMatched = messageContentTypesMatched
        && messages[0].content.includes("LLM Olympics")
        && messages[1].content.includes("数学问答")
        && messages[1].content.includes("只输出最终数字答案");
      const maxTokensMatched = body && typeof body === "object"
        && body.max_tokens === 1
        && !Object.prototype.hasOwnProperty.call(body, "max_completion_tokens");
      summary.messageContentTypesMatched = summary.messageContentTypesMatched
        && messageContentTypesMatched;
      summary.maxTokensMatched = summary.maxTokensMatched && maxTokensMatched;
      summary.modelMatched = summary.modelMatched && modelMatched;
      summary.promptMatched = summary.promptMatched && promptMatched;
      summary.rolesMatched = summary.rolesMatched && rolesMatched;

      if (
        !authorizationMatched
        || !contentTypeHeaderMatched
        || !methodMatched
        || !pathMatched
        || !modelMatched
        || !rolesMatched
        || !messageContentTypesMatched
        || !promptMatched
        || !maxTokensMatched
      ) {
        sendFakeProviderJSON(response, authorizationMatched ? 400 : 401, {
          error: { message: "request rejected", type: "invalid_request_error" },
        });
        return;
      }

      sendFakeProviderJSON(response, 200, {
        choices: [{
          finish_reason: "stop",
          index: 0,
          message: { content: "0", role: "assistant" },
        }],
        created: 0,
        id: "local-test-completion",
        model: "fake-model",
        object: "chat.completion",
        usage: { completion_tokens: 1, prompt_tokens: 1, total_tokens: 2 },
      });
    });
  });
  server.on("clientError", (_error, socket) => {
    summary.transportMatched = false;
    socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  server.removeAllListeners("error");
  server.on("error", () => {
    summary.transportMatched = false;
  });
  server.unref();
  const address = server.address();
  if (!address || typeof address === "string") {
    await new Promise((resolve) => server.close(resolve));
    throw new Error("local fake Provider did not bind a loopback port");
  }

  return {
    baseURL: `http://127.0.0.1:${address.port}`,
    snapshot: () => ({ ...summary }),
    stop: async () => {
      if (!server.listening) return;
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          if (typeof server.closeAllConnections === "function") {
            server.closeAllConnections();
          }
          reject(new Error("local fake Provider did not stop in time"));
        }, 2_000);
        server.close((error) => {
          clearTimeout(timeout);
          if (error) reject(error);
          else resolve();
        });
        if (typeof server.closeIdleConnections === "function") {
          server.closeIdleConnections();
        }
      });
    },
  };
}

async function waitForExit(child, timeoutMs = 5_000) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((resolve) => setTimeout(resolve, timeoutMs)),
  ]);
}

async function directoryContainsSecret(root, secret) {
  if (!secret) return false;
  const needle = Buffer.from(secret, "utf8");
  const visit = async (path) => {
    let entries;
    try {
      entries = await readdir(path, { withFileTypes: true });
    } catch (error) {
      if (error && error.code === "ENOENT") return false;
      throw new Error("temporary control files could not be scanned");
    }
    for (const entry of entries) {
      const candidate = join(path, entry.name);
      if (entry.isDirectory()) {
        if (await visit(candidate)) return true;
      } else if (entry.isFile()) {
        let bytes;
        try {
          bytes = await readFile(candidate);
        } catch (error) {
          if (error && error.code === "ENOENT") continue;
          throw new Error("temporary control files could not be scanned");
        }
        if (bytes.includes(needle)) return true;
      }
    }
    return false;
  };
  return visit(root);
}

async function stopControlServer(server, { cleanup = true } = {}) {
  let failed = false;
  try {
    if (
      server.child
      && server.child.exitCode === null
      && server.child.signalCode === null
    ) {
      server.child.kill("SIGTERM");
      await waitForExit(server.child);
    }
    if (
      server.child
      && server.child.exitCode === null
      && server.child.signalCode === null
    ) {
      server.child.kill("SIGKILL");
      await waitForExit(server.child, 2_000);
    }
  } catch (_error) {
    failed = true;
  }
  try {
    if (server.fakeProvider) await server.fakeProvider.stop();
  } catch (_error) {
    failed = true;
  }
  if (cleanup) {
    try {
      await rm(server.root, { force: true, recursive: true });
    } catch (_error) {
      failed = true;
    }
  }
  if (failed) throw new Error("local Web control fixture did not stop cleanly");
}

async function stopAuditAndCleanControlServer(server) {
  let stopFailed = false;
  let scanFailed = false;
  let credentialPersisted = false;
  let cleanupFailed = false;
  let stderrContainedCredential = false;
  try {
    await stopControlServer(server, { cleanup: false });
  } catch (_error) {
    stopFailed = true;
  }
  stderrContainedCredential = server.stderrContains(server.providerCredential);
  try {
    credentialPersisted = await directoryContainsSecret(
      server.root,
      server.providerCredential,
    );
  } catch (_error) {
    scanFailed = true;
  }
  try {
    await rm(server.root, { force: true, recursive: true });
  } catch (_error) {
    cleanupFailed = true;
  }
  if (credentialPersisted) {
    throw new Error("temporary control files retained a Provider credential");
  }
  if (stderrContainedCredential) {
    throw new Error("server stderr contained a redacted Provider credential");
  }
  if (scanFailed) throw new Error("temporary control files could not be scanned");
  if (cleanupFailed) throw new Error("temporary control files could not be removed");
  if (stopFailed) throw new Error("local Web control fixture did not stop cleanly");
}

async function startControlServer() {
  const executable = process.env.LLMOLYMPIC_WEB_E2E_CLI;
  if (!executable) throw new Error("LLMOLYMPIC_WEB_E2E_CLI is required");

  const root = await mkdtemp(join(tmpdir(), "llmolympic-web-control-e2e-"));
  const database = join(root, "control.db");
  const config = join(root, "config.toml");
  const tokenFile = join(root, "admin.token");
  const providerCredential = randomBytes(32).toString("base64url");
  let child = null;
  let fakeProvider = null;
  try {
    fakeProvider = await startFakeOpenAI(providerCredential);
    const port = await reserveLoopbackPort();
    const baseURL = `http://127.0.0.1:${port}`;
    await writeFile(config, [
      "[profiles.browser-test]",
      'provider = "openai"',
      'default_model = "fake-model"',
      `base_url = "${fakeProvider.baseURL}/v1"`,
      'api_key_env = "E2E_BROWSER_PROFILE_KEY"',
      'display_name = "E2E Provider"',
      "",
      '[pricing."profile:browser-test:fake-model"]',
      'input_usd_per_million_tokens = "0"',
      'output_usd_per_million_tokens = "0"',
      "",
    ].join("\n"), { encoding: "utf8", mode: 0o600 });
    const childEnvironment = { ...process.env };
    for (const name of [
      "ALL_PROXY",
      "HTTPS_PROXY",
      "HTTP_PROXY",
      "all_proxy",
      "https_proxy",
      "http_proxy",
    ]) {
      delete childEnvironment[name];
    }
    delete childEnvironment.E2E_BROWSER_PROFILE_KEY;
    delete childEnvironment.LLMOLYMPIC_CONFIG;
    childEnvironment.NO_PROXY = "127.0.0.1,localhost,::1";
    childEnvironment.no_proxy = "127.0.0.1,localhost,::1";
    child = spawn(
      executable,
      [
        "web",
        "--db",
        database,
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
        "--control-token-file",
        tokenFile,
      ],
      {
        env: {
          ...childEnvironment,
          COLUMNS: "500",
          FORCE_COLOR: "0",
          LLMOLYMPIC_CONFIG: config,
          NO_COLOR: "1",
        },
        // stdout could contain a capability if the CLI regressed. It is never
        // retained or surfaced by this fixture.
        stdio: ["ignore", "ignore", "pipe"],
      },
    );
    child.stderr.setEncoding("utf8");
    let stderr = "";
    let stderrContainedProviderCredential = false;
    child.stderr.on("data", (chunk) => {
      const combined = `${stderr}${chunk}`;
      if (combined.includes(providerCredential)) {
        stderrContainedProviderCredential = true;
      }
      stderr = combined.slice(-8_000);
    });

    const deadline = Date.now() + CONTROL_START_TIMEOUT_MS;
    let adminToken = null;
    let lastStatus = null;
    while (Date.now() < deadline) {
      if (child.exitCode !== null || child.signalCode !== null) break;
      if (adminToken === null) {
        try {
          const candidate = (await readFile(tokenFile, "utf8")).trim();
          if (URL_SAFE_CAPABILITY.test(candidate)) adminToken = candidate;
        } catch (error) {
          if (error && error.code !== "ENOENT") throw error;
        }
      }
      if (adminToken !== null) {
        try {
          const response = await fetch(`${baseURL}/api/v1/control/catalog`, {
            headers: { Authorization: `Bearer ${adminToken}` },
            signal: AbortSignal.timeout(750),
          });
          lastStatus = response.status;
          if (response.ok) {
            return {
              adminToken,
              baseURL,
              child,
              database,
              fakeProvider,
              fakeProviderSummary: () => fakeProvider.snapshot(),
              providerCredential,
              root,
              stderrContains: (secret) => Boolean(
                secret
                && (
                  (secret === providerCredential && stderrContainedProviderCredential)
                  || stderr.includes(secret)
                )
              ),
            };
          }
        } catch (_error) {
          // The server may still be binding the loopback socket.
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(
      `local Web control server did not become ready` +
        (lastStatus === null ? "" : ` (HTTP ${lastStatus})`) +
        `\nstderr: ${redactSensitiveText(stderr, [adminToken, providerCredential])}` +
        "\nserver stdout withheld because it may contain a capability",
    );
  } catch (error) {
    try {
      await stopControlServer({ child, fakeProvider, root });
    } catch (_cleanupError) {
      throw new Error("local Web control fixture setup could not be cleaned up");
    }
    throw error;
  }
}

const test = base.extend({
  controlServer: async ({}, use) => {
    const server = await startControlServer();
    try {
      await use(server);
    } finally {
      await stopAuditAndCleanControlServer(server);
    }
  },
});

async function openAdminPage(browser, server, pathname = "/new") {
  const secrets = [server.adminToken];
  const monitor = { errors: [], messages: [], sensitive: false };
  const context = await browser.newContext({
    baseURL: server.baseURL,
    colorScheme: "light",
    reducedMotion: "reduce",
    viewport: { height: 900, width: 1280 },
  });
  try {
    await context.addInitScript(
      ({ key, origin, token }) => {
        if (window.location.origin === origin) window.sessionStorage.setItem(key, token);
      },
      { key: ADMIN_STORAGE_KEY, origin: server.baseURL, token: server.adminToken },
    );
  } catch (_error) {
    await context.close();
    throw new Error("browser could not stage a local admin credential");
  }
  const page = await context.newPage();
  monitorBrowserMessages(page, monitor, secrets);
  await page.goto(pathname);
  expect(await page.evaluate(() => window.location.hash)).toBe("");
  return {
    browserMessages: monitor.messages,
    context,
    errors: monitor.errors,
    page,
    secrets,
    sensitiveMessageSeen: () => monitor.sensitive,
  };
}

async function configureMathQuiz(page, { humanName = null } = {}) {
  await expect(page.getByRole("heading", { name: "新建比赛 / 任务" })).toBeVisible();
  await page.getByLabel("比赛项目").selectOption("math_quiz");
  await page.locator("#player-0-kind").selectOption(humanName ? "human" : "mock");
  if (humanName) {
    await page.locator("#player-0-name").fill(humanName);
  } else {
    await page.locator("#player-0-strategy").selectOption("fixed");
  }
  await page.locator("#player-1-kind").selectOption("mock");
  await page.locator("#player-1-strategy").selectOption("illegal");
  await page.getByLabel("每场回合数").fill("1");
  await page.getByLabel("随机种子").fill("4242");
}

test.describe("ephemeral Provider credentials", () => {
  test("sets, keeps in service memory, and clears a named Profile Key", async ({
    browser,
    controlServer,
  }) => {
    const sentinel = controlServer.providerCredential;
    const admin = await openAdminPage(browser, controlServer);
    admin.secrets.push(sentinel);
    try {
      const keyInput = admin.page.getByLabel("E2E Provider 的 API Key");
      const applyButton = admin.page.getByRole("button", { name: "应用 Key" });
      const announcement = admin.page.locator(
        "#profile-credential-browser-test-announcement",
      );
      await expect(keyInput).toBeVisible();
      await expect(keyInput).toHaveAttribute("type", "password");
      await expect(keyInput).toHaveAttribute("autocomplete", "new-password");
      await expect(admin.page.getByText("Key 未就绪", { exact: true })).toBeVisible();
      await expect(announcement).toHaveText("");
      const announcementHandle = await announcement.elementHandle();
      expect(announcementHandle).not.toBeNull();

      await keyInput.fill(" leading-space");
      await applyButton.click();
      await expect(keyInput).toHaveAttribute("aria-invalid", "true");
      await expect(admin.page.getByText(
        "API Key 只能包含不带空白的可打印 ASCII 字符。",
        { exact: true },
      )).toBeVisible();
      await expect(keyInput).toBeFocused();

      await keyInput.fill("密钥");
      await expect(keyInput).toHaveAttribute("aria-invalid", "true");
      await expect(admin.page.getByText(
        "API Key 只能包含不带空白的可打印 ASCII 字符。",
        { exact: true },
      )).toBeVisible();

      await keyInput.fill("");
      await applyButton.click();
      await expect(keyInput).toHaveAttribute("aria-invalid", "true");
      await expect(admin.page.getByText("请输入 API Key。", { exact: true })).toBeVisible();

      await keyInput.fill(sentinel);
      await expect(keyInput).not.toHaveAttribute("aria-invalid", "true");

      const applied = admin.page.waitForResponse((response) => (
        response.request().method() === "PUT"
        && response.url().endsWith("/api/v1/control/profiles/browser-test/credential")
      ));
      await applyButton.click();
      expect((await applied).status()).toBe(204);

      await expect(keyInput).toHaveCount(0);
      await expect(admin.page.getByText("Key 已就绪", { exact: true })).toBeVisible();
      await expect(announcement).toHaveText("E2E Provider 的 Key 已应用。");
      await expect(admin.page.getByRole("button", { name: "清除本次 Key" })).toBeFocused();
      expect(await announcement.evaluate(
        (node, original) => node === original,
        announcementHandle,
      )).toBeTruthy();
      expect(await admin.context.cookies()).toEqual([]);
      const browserState = await admin.page.evaluate(() => ({
        html: document.documentElement.outerHTML,
        localStorage: Object.entries(window.localStorage),
        sessionStorage: Object.entries(window.sessionStorage),
        url: window.location.href,
      }));
      const browserStateRetainedCredential = [
        browserState.html,
        JSON.stringify(browserState.localStorage),
        JSON.stringify(browserState.sessionStorage),
        browserState.url,
      ].some((value) => value.includes(sentinel));
      expect(
        browserStateRetainedCredential,
        "browser state retained a redacted Provider credential",
      ).toBe(false);
      expect(browserState.sessionStorage.some(([key]) => key === "llmolympic.control.admin")).toBeTruthy();

      await admin.page.reload();
      await expect(admin.page.getByText("Key 已就绪", { exact: true })).toBeVisible();
      await expect(admin.page.getByLabel("E2E Provider 的 API Key")).toHaveCount(0);
      await expectNoWcagViolations(admin.page);

      const cleared = admin.page.waitForResponse((response) => (
        response.request().method() === "DELETE"
        && response.url().endsWith("/api/v1/control/profiles/browser-test/credential")
      ));
      await admin.page.getByRole("button", { name: "清除本次 Key" }).click();
      expect((await cleared).status()).toBe(204);

      const restoredInput = admin.page.getByLabel("E2E Provider 的 API Key");
      await expect(admin.page.getByText("Key 未就绪", { exact: true })).toBeVisible();
      await expect(restoredInput).toBeVisible();
      await expect(restoredInput).toHaveValue("");
      await expect(restoredInput).toHaveAttribute("type", "password");
      await expect(restoredInput).toBeFocused();
      await expect(announcement).toHaveText("E2E Provider 的 Key 已清除。");
      await expectNoWcagViolations(admin.page);
      expect(admin.errors).toEqual([]);
    } finally {
      expect.soft(
        admin.sensitiveMessageSeen(),
        "browser messages contained a redacted Provider credential",
      ).toBe(false);
      expect.soft(
        controlServer.stderrContains(sentinel),
        "server stderr contained a redacted Provider credential",
      ).toBe(false);
      await admin.context.close();
    }
  });

  test("runs a real worker through a local fake OpenAI endpoint", async ({
    browser,
    controlServer,
  }) => {
    test.setTimeout(CONTROL_TEST_TIMEOUT_MS);
    const credential = controlServer.providerCredential;
    const admin = await openAdminPage(browser, controlServer);
    admin.secrets.push(credential);
    try {
      const keyInput = admin.page.getByLabel("E2E Provider 的 API Key");
      await keyInput.fill(credential);
      const applied = admin.page.waitForResponse((response) => (
        response.request().method() === "PUT"
        && response.url().endsWith("/api/v1/control/profiles/browser-test/credential")
      ));
      await admin.page.getByRole("button", { name: "应用 Key" }).click();
      expect((await applied).status()).toBe(204);
      await expect(admin.page.getByText("Key 已就绪", { exact: true })).toBeVisible();

      await admin.page.getByLabel("比赛项目").selectOption("math_quiz");
      await admin.page.locator("#player-0-kind").selectOption("profile");
      await admin.page.locator("#player-0-profile").selectOption("browser-test");
      await admin.page.locator("#player-1-kind").selectOption("mock");
      await admin.page.locator("#player-1-strategy").selectOption("fixed");
      await admin.page.getByLabel("每场回合数").fill("1");
      await admin.page.getByLabel("随机种子").fill("7331");
      await admin.page.getByLabel("最大调用数").fill("2");
      await admin.page.getByLabel("最大输入 Token").fill("4096");
      await admin.page.getByLabel("单次最大输出 Token").fill("1");
      await admin.page.getByLabel("累计最大输出 Token").fill("1");
      await admin.page.getByLabel("最大预估成本（USD）").fill("0");

      await prepareAndStart(admin.page);
      await expect(admin.page.locator(
        ".job-status.status-completed, .job-status.status-failed",
      )).toBeVisible({ timeout: 25_000 });
      const providerSummary = controlServer.fakeProviderSummary();
      expect(providerSummary, "fake Provider safe contract summary").toEqual({
        authorizationMatched: true,
        bodyCredentialFree: true,
        bodyWithinLimit: true,
        contentTypeHeaderMatched: true,
        jsonMatched: true,
        maxTokensMatched: true,
        messageContentTypesMatched: true,
        methodMatched: true,
        modelMatched: true,
        pathMatched: true,
        promptMatched: true,
        requestCount: 1,
        rolesMatched: true,
        transportMatched: true,
      });
      const { archivePath } = await waitForCompletedJob(admin.page);

      const jobId = new URL(admin.page.url()).pathname.split("/").pop();
      const jobResponse = await admin.page.request.get(
        `${controlServer.baseURL}/api/v1/control/jobs/${encodeURIComponent(jobId)}`,
        { headers: { Authorization: `Bearer ${controlServer.adminToken}` } },
      );
      expect(jobResponse.ok()).toBeTruthy();
      const jobText = await jobResponse.text();
      expect(
        jobText.includes(credential),
        "control job response retained a redacted Provider credential",
      ).toBe(false);
      const { job } = JSON.parse(jobText);
      expect(job).toEqual(expect.objectContaining({
        failure_code: null,
        final_kind: "match",
        status: "completed",
      }));
      expect(job.final_match_ids).toHaveLength(1);

      const detailResponse = await admin.page.request.get(`/api/v1${archivePath}`);
      expect(detailResponse.ok()).toBeTruthy();
      const archiveText = await detailResponse.text();
      expect(
        archiveText.includes(credential),
        "public archive retained a redacted Provider credential",
      ).toBe(false);
      const detail = JSON.parse(archiveText);
      expect(detail.match).toEqual(expect.objectContaining({
        game: "math_quiz",
        players: ["E2E Provider", "mock:fixed"],
        rated: true,
      }));
      expect(job.final_match_ids).toEqual([detail.match.match_id]);
      expect(
        detail.events.some((event) => (
          event.type === "move_received"
          && event.player === "E2E Provider"
          && event.data && event.data.move === "0"
        )),
        "public archive omitted the fake Provider move",
      ).toBe(true);
      expect(
        await directoryContainsSecret(controlServer.root, credential),
        "running control files retained a redacted Provider credential",
      ).toBe(false);

      const browserStateRetainedCredential = await admin.page.evaluate((secret) => (
        window.location.href.includes(secret)
        || document.documentElement.outerHTML.includes(secret)
        || JSON.stringify(Object.entries(window.localStorage)).includes(secret)
        || JSON.stringify(Object.entries(window.sessionStorage)).includes(secret)
      ), credential);
      expect(
        browserStateRetainedCredential,
        "browser state retained a redacted Provider credential",
      ).toBe(false);
      expect(admin.errors).toEqual([]);
    } finally {
      expect.soft(
        admin.sensitiveMessageSeen(),
        "browser messages contained a redacted Provider credential",
      ).toBe(false);
      expect.soft(
        controlServer.stderrContains(credential),
        "server stderr contained a redacted Provider credential",
      ).toBe(false);
      await admin.context.close();
    }
  });
});

async function configureMathQuizChampionship(page) {
  await expect(page.getByRole("heading", { name: "新建比赛 / 任务" })).toBeVisible();
  await page.locator("#control-mode").selectOption("championship");
  await page.getByLabel("比赛项目").selectOption("math_quiz");
  await expect(page.locator(".roster-card")).toHaveCount(4);
  const strategies = ["random", "fixed", "illegal", "balanced"];
  for (const [index, strategy] of strategies.entries()) {
    await expect(page.locator(`#player-${index}-kind option[value="human"]`)).toHaveCount(0);
    await page.locator(`#player-${index}-kind`).selectOption("mock");
    await page.locator(`#player-${index}-strategy`).selectOption(strategy);
  }
  await page.getByLabel("每场回合数").fill("1");
  await page.getByLabel("随机种子").fill("5150");
}

async function prepareAndStart(page) {
  await page.getByRole("button", { name: "生成准备态预览" }).click();
  await expect(page.getByRole("heading", { name: "确认后才会启动" })).toBeVisible();
  await expect(page.getByText("等待确认", { exact: true })).toBeVisible();
  await expectNoWcagViolations(page);
  await page.getByRole("button", { name: "确认并启动" }).click();
  await expect(page).toHaveURL(/\/jobs\/[A-Za-z0-9._:-]+$/);
}

async function waitForCompletedJob(page) {
  await expect(page.locator(".job-status.status-completed")).toBeVisible({
    timeout: 25_000,
  });
  const archiveLink = page.getByRole("link", { name: "打开存档回放" });
  await expect(archiveLink).toBeVisible();
  const archivePath = await archiveLink.getAttribute("href");
  if (!archivePath || !/^\/matches\/[A-Za-z0-9._:-]+$/.test(archivePath)) {
    throw new Error("completed Web job did not expose a safe archive path");
  }
  return { archiveLink, archivePath };
}

async function extractParticipationCredential(page) {
  const link = page.locator(".seat-link-list a").first();
  const href = await link.first().getAttribute("href");
  if (!href) throw new Error("human Web job did not expose a participation entry");
  let parsed;
  try {
    parsed = new URL(href, "http://127.0.0.1");
  } catch (_error) {
    throw new Error("human Web job exposed an invalid participation entry");
  }
  const match = parsed.pathname.match(PARTICIPATION_PATH);
  const parameters = new URLSearchParams(parsed.hash.slice(1));
  const token = parameters.get("capability");
  if (
    !match
    || parameters.size !== 1
    || !URL_SAFE_CAPABILITY.test(token || "")
  ) {
    throw new Error("human Web job exposed an invalid participation entry");
  }
  return {
    capability: token,
    pathname: parsed.pathname,
    seatId: match[2],
    sessionId: match[1],
  };
}

test("full Web control prepares, confirms, runs, archives, and replays a mock match", async ({
  browser,
  controlServer,
}) => {
  test.setTimeout(CONTROL_TEST_TIMEOUT_MS);
  const admin = await openAdminPage(browser, controlServer);
  try {
    await configureMathQuiz(admin.page);
    await expectNoWcagViolations(admin.page);
    await prepareAndStart(admin.page);

    const { archiveLink, archivePath } = await waitForCompletedJob(admin.page);
    await expectNoWcagViolations(admin.page);

    const detailResponse = await admin.page.request.get(`/api/v1${archivePath}`);
    expect(detailResponse.ok()).toBeTruthy();
    const detail = await detailResponse.json();
    expect(detail.match).toEqual(expect.objectContaining({
      game: "math_quiz",
      players: ["mock:fixed", "mock:illegal"],
      rated: true,
    }));

    await archiveLink.click();
    await expect(admin.page).toHaveURL(new RegExp(`${archivePath}$`));
    await expect(admin.page.getByText("WebSocket 同源回放", { exact: true })).toBeVisible();
    await expectNoWcagViolations(admin.page);
    expect(admin.errors).toEqual([]);
  } finally {
    await admin.context.close();
  }
});

test("full Web control starts a browser Human seat in a second context and archives it", async ({
  browser,
  controlServer,
}) => {
  test.setTimeout(CONTROL_TEST_TIMEOUT_MS);
  const humanName = "Web 控制 E2E";
  const admin = await openAdminPage(browser, controlServer);
  let participantContext = null;
  try {
    await configureMathQuiz(admin.page, { humanName });
    await expectNoWcagViolations(admin.page);
    await prepareAndStart(admin.page);

    const seatLink = admin.page.locator(".seat-link-list a").first();
    await expect(seatLink).toBeVisible({ timeout: 10_000 });
    const participation = await extractParticipationCredential(admin.page);
    admin.secrets.push(participation.capability);

    const participantMonitor = { errors: [], messages: [], sensitive: false };
    participantContext = await browser.newContext({
      baseURL: controlServer.baseURL,
      colorScheme: "light",
      reducedMotion: "reduce",
      viewport: { height: 900, width: 1280 },
    });
    try {
      await participantContext.addInitScript(
        ({ key, origin, token }) => {
          if (window.location.origin === origin) window.sessionStorage.setItem(key, token);
        },
        {
          key: `llmolympic.participation.${participation.sessionId}.${participation.seatId}`,
          origin: controlServer.baseURL,
          token: participation.capability,
        },
      );
    } catch (_error) {
      throw new Error("browser could not stage a local participation credential");
    }
    const participantPage = await participantContext.newPage();
    monitorBrowserMessages(
      participantPage,
      participantMonitor,
      [controlServer.adminToken, participation.capability],
    );
    await participantPage.goto(participation.pathname);
    await expect(participantPage).toHaveURL(
      /\/participate\/[A-Za-z0-9._:-]+\/[A-Za-z0-9._:-]+$/,
    );
    expect(await participantPage.evaluate(() => window.location.hash)).toBe("");
    await expect(participantPage.getByRole("heading", { name: humanName })).toBeVisible();
    await expect(participantPage.locator(".participation-prompt")).toContainText("数学问答");
    await expectNoWcagViolations(participantPage);

    await participantPage.getByLabel("输入动作或答案").fill("2");
    const submission = participantPage.waitForResponse(
      (response) => response.request().method() === "POST"
        && response.url().includes("/submissions"),
    );
    await participantPage.getByRole("button", { name: "提交本轮输入" }).click();
    expect((await submission).status()).toBe(202);
    await expect(
      participantPage.getByRole("heading", { name: "比赛已完成" }),
    ).toBeVisible({ timeout: 20_000 });
    await expect.poll(
      () => participantPage.evaluate(() => window.sessionStorage.length),
    ).toBe(0);

    const { archiveLink, archivePath } = await waitForCompletedJob(admin.page);
    const detailResponse = await admin.page.request.get(`/api/v1${archivePath}`);
    expect(detailResponse.ok()).toBeTruthy();
    const detail = await detailResponse.json();
    expect(detail.match).toEqual(expect.objectContaining({
      game: "math_quiz",
      players: [humanName, "mock:illegal"],
      rated: true,
    }));
    if (JSON.stringify(detail).includes(participation.capability)) {
      throw new Error("archive unexpectedly retained a participation capability");
    }

    await archiveLink.click();
    await expect(admin.page).toHaveURL(new RegExp(`${archivePath}$`));
    await expect(admin.page.getByText("WebSocket 同源回放", { exact: true })).toBeVisible();
    await expectNoWcagViolations(admin.page);
    expect(admin.errors).toEqual([]);
    expect(participantMonitor.errors).toEqual([]);
  } finally {
    if (participantContext !== null) await participantContext.close();
    await admin.context.close();
  }
});

test("full Web control runs a four-player championship and exposes its final live bracket", async ({
  browser,
  controlServer,
}) => {
  test.setTimeout(CONTROL_TEST_TIMEOUT_MS);
  const admin = await openAdminPage(browser, controlServer);
  try {
    await configureMathQuizChampionship(admin.page);
    await expectNoWcagViolations(admin.page);
    await prepareAndStart(admin.page);

    await expect(admin.page.locator(".job-status.status-completed")).toBeVisible({
      timeout: 40_000,
    });
    const archiveLinks = admin.page.locator(".archive-button-list a");
    await expect(archiveLinks).toHaveCount(6);
    const liveLink = admin.page.getByRole("link", { name: "打开实时观战" });
    await expect(liveLink).toBeVisible();
    const livePath = await liveLink.getAttribute("href");
    if (!livePath || !/^\/live\/[A-Za-z0-9._:-]+$/.test(livePath)) {
      throw new Error("completed championship did not expose a safe live path");
    }

    const jobId = new URL(admin.page.url()).pathname.split("/").pop();
    const jobResponse = await admin.page.request.get(
      `${controlServer.baseURL}/api/v1/control/jobs/${encodeURIComponent(jobId)}`,
      { headers: { Authorization: `Bearer ${controlServer.adminToken}` } },
    );
    expect(jobResponse.ok()).toBeTruthy();
    const { job } = await jobResponse.json();
    expect(job).toEqual(expect.objectContaining({
      championship_id: expect.any(String),
      final_kind: "championship",
      resumable: false,
      status: "completed",
    }));
    expect(job.spec).toEqual(expect.objectContaining({
      game: "math_quiz",
      mode: "championship",
      resume_championship_id: null,
    }));
    expect(job.spec.players).toHaveLength(4);
    expect(job.final_match_ids).toHaveLength(6);

    const liveResponse = await admin.page.request.get(`/api/v1${livePath}`);
    expect(liveResponse.ok()).toBeTruthy();
    const liveDetail = await liveResponse.json();
    expect(liveDetail.match).toEqual(expect.objectContaining({
      final_id: job.championship_id,
      final_kind: "championship",
      mode: "championship",
      status: "completed",
    }));
    expect(liveDetail.match.championship_bracket).toEqual(expect.objectContaining({
      champion: expect.any(String),
      championship_id: job.championship_id,
      pairing_count: 3,
      player_count: 4,
    }));
    expect(liveDetail.match.championship_bracket.pairings).toHaveLength(3);

    await liveLink.click();
    await expect(admin.page).toHaveURL(new RegExp(`${livePath}$`));
    await expect(admin.page.getByRole("heading", { name: "淘汰赛对阵" })).toBeVisible();
    await expect(admin.page.locator(".championship-pairing")).toHaveCount(3);
    await expect(admin.page.locator(".championship-champion")).toContainText(
      liveDetail.match.championship_bracket.champion,
    );
    await expect(admin.page.locator(".archive-actions a")).toHaveCount(6);
    await expectNoWcagViolations(admin.page);
    expect(admin.errors).toEqual([]);
  } finally {
    await admin.context.close();
  }
});
