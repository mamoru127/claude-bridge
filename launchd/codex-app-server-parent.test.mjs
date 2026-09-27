import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const wrapper = fileURLToPath(new URL("./codex-app-server-parent.mjs", import.meta.url));

const waitFor = async (condition, timeoutMs = 3_000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await condition()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("timed out waiting for condition");
};

const runReplacementCase = async (termHandler) => {
  const directory = await mkdtemp(join(tmpdir(), "codex-app-server-parent-"));
  const watchedNode = join(directory, "codex");
  const replacementNode = join(directory, "replacement");
  const childScript = join(directory, "child.mjs");
  const childPidPath = join(directory, "child.pid");
  await writeFile(watchedNode, '#!/bin/sh\nexec "$WATCHED_NODE" "$@"\n');
  await chmod(watchedNode, 0o755);
  await writeFile(
    childScript,
    `import { writeFileSync } from "node:fs";\n` +
      `writeFileSync(process.argv[2], String(process.pid));\n` +
      `process.on("SIGTERM", ${termHandler});\n` +
      `setInterval(() => {}, 1_000);\n`,
  );

  const parent = spawn(process.execPath, [wrapper, watchedNode, childScript, childPidPath], {
    env: {
      ...process.env,
      WATCHED_NODE: process.execPath,
      CODEX_APP_SERVER_WATCH_INTERVAL_MS: "20",
      CODEX_APP_SERVER_SHUTDOWN_GRACE_MS: "100",
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  parent.stderr.setEncoding("utf8");
  parent.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  let childPid;
  try {
    await waitFor(async () => {
      try {
        return Number(await readFile(childPidPath, "utf8"));
      } catch {
        return false;
      }
    });
    childPid = Number(await readFile(childPidPath, "utf8"));
    await writeFile(replacementNode, '#!/bin/sh\n# replaced\nexec "$WATCHED_NODE" "$@"\n');
    await chmod(replacementNode, 0o755);
    await rename(replacementNode, watchedNode);

    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("wrapper did not exit")), 3_000);
      parent.on("exit", (code, signal) => {
        clearTimeout(timer);
        resolve({ code, signal });
      });
    });
    return { childPid, result, stderr };
  } finally {
    if (childPid) {
      try {
        process.kill(-childPid, "SIGKILL");
      } catch (error) {
        if (error.code !== "ESRCH") {
          throw error;
        }
      }
    }
    if (parent.exitCode === null && parent.signalCode === null) {
      parent.kill("SIGKILL");
    }
    await rm(directory, { recursive: true });
  }
};

test("更新後にSIGTERMで終了しないapp-serverをプロセスグループごと強制終了する", async () => {
  const { childPid, result, stderr } = await runReplacementCase("() => {}");
  assert.deepEqual(result, { code: 0, signal: null });
  assert.match(stderr, /codex executable was replaced/);
  assert.match(stderr, /forcing process group shutdown/);
  assert.throws(() => process.kill(childPid, 0), { code: "ESRCH" });
});

test("更新後にapp-serverが正常終了すれば強制終了しない", async () => {
  const { result, stderr } = await runReplacementCase("() => process.exit(0)");
  assert.deepEqual(result, { code: 0, signal: null });
  assert.match(stderr, /codex executable was replaced/);
  assert.doesNotMatch(stderr, /forcing process group shutdown/);
});
