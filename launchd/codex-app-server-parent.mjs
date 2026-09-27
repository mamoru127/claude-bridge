import { spawn } from "node:child_process";
import { statSync } from "node:fs";

const [codexPath, ...codexArgs] = process.argv.slice(2);

if (!codexPath) {
  console.error("codex executable path is required");
  process.exit(2);
}

// Codex アプリを更新すると、内蔵の codex 実行ファイルが差し替わる。走り続けている
// app-server はそのままでは古い実行ファイルのままで、アプリとバージョンがずれるだけでなく、
// macOS のファイルアクセス許可が差し替え前のプロセスに紐づいたままになる。そうなると
// ~/Documents 配下の AGENTS.md と .agents/skills の走査が Operation not permitted で
// 失敗し、プロジェクトのスキルが `/` コマンドから消える（ログは残るがアプリには出ない）。
// 差し替えを見つけたら子を落として自分も終わり、KeepAlive の再起動で入れ替える。
// app-server は実行中のターンを待って SIGTERM だけでは終了しないことがあるため、猶予を
// 過ぎたら子のプロセスグループを強制終了し、古いソケットを確実に手放す。
const readMilliseconds = (name, fallback) => {
  const raw = process.env[name];
  if (raw === undefined) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    console.error(`${name} must be a positive integer`);
    process.exit(2);
  }
  return value;
};

const WATCH_INTERVAL_MS = readMilliseconds("CODEX_APP_SERVER_WATCH_INTERVAL_MS", 60_000);
const SHUTDOWN_GRACE_MS = readMilliseconds("CODEX_APP_SERVER_SHUTDOWN_GRACE_MS", 10_000);

const identify = () => {
  const stat = statSync(codexPath);
  return `${stat.ino}:${stat.size}:${stat.mtimeMs}`;
};

const startedWith = identify();

const child = spawn(codexPath, codexArgs, {
  detached: true,
  env: process.env,
  stdio: "inherit",
});

let forceTimer;
let stopping = false;

const signalChildGroup = (signal) => {
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== "ESRCH") {
      throw error;
    }
  }
};

const stopChild = (signal, message) => {
  if (stopping) {
    return;
  }
  stopping = true;
  clearInterval(watcher);
  console.error(message);
  signalChildGroup(signal);
  forceTimer = setTimeout(() => {
    console.error("codex app-server did not exit in time; forcing process group shutdown");
    signalChildGroup("SIGKILL");
  }, SHUTDOWN_GRACE_MS);
};

const watcher = setInterval(() => {
  let current;
  try {
    current = identify();
  } catch {
    // 更新中は実行ファイルが一時的に消える。差し替え後の姿を次の周回で見る。
    return;
  }
  if (current === startedWith) {
    return;
  }
  stopChild("SIGTERM", "codex executable was replaced; restarting app-server");
}, WATCH_INTERVAL_MS);

for (const signal of ["SIGINT", "SIGHUP", "SIGTERM"]) {
  process.on(signal, () => {
    stopChild(signal, `received ${signal}; stopping codex app-server`);
  });
}

child.on("error", (error) => {
  console.error(`failed to start codex app-server: ${error.message}`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  clearInterval(watcher);
  clearTimeout(forceTimer);
  if (stopping) {
    process.exit(0);
  }
  if (signal) {
    console.error(`codex app-server exited with signal ${signal}`);
    process.exit(1);
  }
  process.exit(code ?? 1);
});
