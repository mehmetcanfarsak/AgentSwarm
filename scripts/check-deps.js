#!/usr/bin/env node
// Dependency doctor for Agentainer.
//
// Runs on `npm install` (postinstall) and on demand via `agentainer doctor`.
// It only checks the things Agentainer itself needs -- python3 and tmux -- and
// reports on optional agent CLIs (claude, codex, gemini, ...) without ever
// treating their absence as an error: a user may only ever run one of them.
//
// This script NEVER fails the install. Missing tools are reported with hints;
// the exit code stays 0 so `npm install -g agentainer` always succeeds.
"use strict";

const { spawnSync } = require("child_process");
const os = require("os");

function has(cmd, args) {
  const probe = spawnSync(cmd, args, { stdio: "ignore" });
  return !probe.error && probe.status === 0;
}

const platform = os.platform();
function installHint(pkg) {
  if (platform === "darwin") return `brew install ${pkg}`;
  if (platform === "linux")
    return `sudo apt install ${pkg}   (or your distro's package manager)`;
  if (platform === "win32")
    return `${pkg} is not natively supported on Windows; use WSL2`;
  return `install ${pkg} with your package manager`;
}

// --- Required: Agentainer cannot run without these -------------------------
const required = [
  { name: "python3", ok: has("python3", ["--version"]) || has("python", ["--version"]), hint: installHint("python3") },
  { name: "tmux", ok: has("tmux", ["-V"]), hint: installHint("tmux") },
];

// --- Optional: at least one agent CLI, but which one is up to the user ------
const optional = [
  { name: "claude", label: "Claude Code", ok: has("claude", ["--version"]) },
  { name: "codex", label: "Codex CLI", ok: has("codex", ["--version"]) },
  { name: "gemini", label: "Gemini CLI", ok: has("gemini", ["--version"]) },
];

const missingRequired = required.filter((r) => !r.ok);

process.stdout.write("\nAgentainer -- checking dependencies\n");
process.stdout.write("-----------------------------------\n");

for (const r of required) {
  process.stdout.write(`  [${r.ok ? "ok" : "--"}] ${r.name}${r.ok ? "" : `   -> ${r.hint}`}\n`);
}

process.stdout.write("\n  Agent CLIs (install whichever you'll actually use):\n");
for (const o of optional) {
  process.stdout.write(`  [${o.ok ? "ok" : "  "}] ${o.name.padEnd(8)} ${o.label}\n`);
}

if (missingRequired.length) {
  process.stdout.write(
    "\n!! Missing required tools: " +
      missingRequired.map((r) => r.name).join(", ") +
      "\n   Install them, then re-check with:  agentainer doctor\n"
  );
} else {
  const anyAgent = optional.some((o) => o.ok);
  process.stdout.write(
    "\nok Core dependencies satisfied." +
      (anyAgent ? "" : " (No agent CLI detected yet -- install one to start a swarm.)") +
      "\n"
  );
}
process.stdout.write("\n");

// Always succeed: never abort an npm install over a missing optional (or even
// required) tool. The launcher re-checks at runtime and fails clearly there.
process.exit(0);
