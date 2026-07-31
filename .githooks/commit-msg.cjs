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
  // A gate that could not run has not approved anything. `status ?? 0` would
  // turn a failed spawn into a clean pass -- the bug that made this repo's own
  // pre-push unreachable for a whole commit.
  if (res.error || res.status === null) {
    console.error(
      "\n[commit-msg] a regra global da maquina nao pode ser executada " +
        `(${res.error ? res.error.code : "sem status"}).`
    );
    process.exit(1);
  }
  process.exit(res.status);
}

process.exit(0);
