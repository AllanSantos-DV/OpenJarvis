#!/usr/bin/env node
// Chained pre-push: the machine's global gate first, then this repo's.
//
// Why this file exists rather than a plain `.git/hooks/pre-push`: this machine
// sets `core.hooksPath` globally, so Git ignores `.git/hooks/` entirely. The
// global dispatcher does try to delegate to the local hook, but on Windows it
// spawns the shell script directly and gets ENOENT -- `status` comes back null,
// `null ?? 0` becomes exit 0, and the local gate silently never runs. Measured,
// after shipping a gate that was unreachable.
//
// So the repo points `core.hooksPath` at its own directory and takes
// responsibility for BOTH: the owner's global rules still run, first, and a
// veto from them still blocks. Nothing is lost by taking over.

const { spawnSync } = require("node:child_process");
const { existsSync, readFileSync } = require("node:fs");
const { join } = require("node:path");

const args = process.argv.slice(2);
let stdin = Buffer.alloc(0);
try {
  stdin = readFileSync(0);
} catch {
  /* no stdin: a manual run */
}

// 1. The machine-wide gate. It enforces rules unrelated to this repo and must
//    keep working.
const globalDispatch = join(
  process.env.USERPROFILE || process.env.HOME || "",
  ".copilot",
  "githooks",
  "dispatch.mjs"
);
if (existsSync(globalDispatch)) {
  const res = spawnSync(process.execPath, [globalDispatch, ...args], {
    input: stdin,
    stdio: ["pipe", "inherit", "inherit"],
  });
  if (res.status) process.exit(res.status);
}

// 2. This repo's gate: the contract tests CI structurally cannot run.
const own = join(__dirname, "..", "scripts", "hooks", "pre-push.cjs");
if (existsSync(own)) {
  const res = spawnSync(process.execPath, [own, ...args], {
    input: stdin,
    stdio: ["pipe", "inherit", "inherit"],
  });
  process.exit(res.status ?? 0);
}

process.exit(0);
