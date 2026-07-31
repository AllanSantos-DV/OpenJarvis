#!/usr/bin/env node
// Chained commit-msg: the machine's global rule first, then nothing of ours.
//
// This file exists only because the repo took over `core.hooksPath` to make its
// pre-push gate reachable. Taking over a hooks directory takes over ALL of it:
// the moment `.githooks` became the path, the machine's own commit-msg stopped
// running and a rule the owner enforces globally silently lapsed.
//
// Caught by his own pre-push gate one commit later, which is the good outcome
// -- but the lesson is that overriding a shared mechanism means re-providing
// everything it did, not just the part you wanted.

const { spawnSync } = require("node:child_process");
const { existsSync } = require("node:fs");
const { join } = require("node:path");

const args = process.argv.slice(2);
const globalHook = join(
  process.env.USERPROFILE || process.env.HOME || "",
  ".copilot",
  "githooks",
  "commit-msg.dispatch.mjs"
);

if (existsSync(globalHook)) {
  const res = spawnSync(process.execPath, [globalHook, ...args], {
    stdio: "inherit",
  });
  process.exit(res.status ?? 0);
}

process.exit(0);
