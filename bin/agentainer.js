#!/usr/bin/env node
// Agentainer -- global CLI launcher.
//
// npm symlinks this file into a bin directory (e.g. /usr/local/bin/agentainer).
// Node resolves that symlink before setting __dirname, so __dirname always
// points at the real bin/ inside the installed package -- which makes the
// package root, and therefore SWARM_HOME, reliable no matter where npm puts us.
"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

const PKG_ROOT = path.resolve(__dirname, "..");
const ENTRY = path.join(PKG_ROOT, "lib", "swarm.py");

// `agentainer doctor` re-runs the dependency check without touching Python.
if (process.argv[2] === "doctor") {
  const r = spawnSync(process.execPath, [path.join(PKG_ROOT, "scripts", "check-deps.js")], {
    stdio: "inherit",
  });
  process.exit(r.status === null ? 1 : r.status);
}

// Locate a Python interpreter, mirroring ./agentainer (SWARM_PYTHON, then python3,
// then python).
function findPython() {
  const candidates = process.env.SWARM_PYTHON
    ? [process.env.SWARM_PYTHON]
    : ["python3", "python"];
  for (const cand of candidates) {
    const probe = spawnSync(cand, ["--version"], { stdio: "ignore" });
    if (!probe.error && probe.status === 0) return cand;
  }
  return null;
}

function has(cmd, args) {
  const probe = spawnSync(cmd, args, { stdio: "ignore" });
  return !probe.error && probe.status === 0;
}

const python = findPython();
if (!python) {
  process.stderr.write(
    "xx Agentainer needs python3 on PATH (or set SWARM_PYTHON).\n" +
      "   Run `agentainer doctor` for install hints.\n"
  );
  process.exit(1);
}

// tmux is required for everything except `validate`; warn but don't block, so
// `agentainer validate` and `agentainer doctor` still work without it.
if (!has("tmux", ["-V"])) {
  process.stderr.write(
    "!! tmux was not found on PATH; every command except 'validate' will fail.\n" +
      "   Run `agentainer doctor` for install hints.\n"
  );
}

const result = spawnSync(python, [ENTRY, ...process.argv.slice(2)], {
  stdio: "inherit",
  env: { ...process.env, SWARM_HOME: PKG_ROOT },
});

if (result.error) {
  process.stderr.write(`xx failed to launch Agentainer: ${result.error.message}\n`);
  process.exit(1);
}
if (result.signal) {
  // Re-raise the signal so the parent shell sees the real cause (e.g. Ctrl-C).
  process.kill(process.pid, result.signal);
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
