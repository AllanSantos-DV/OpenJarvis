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

// A failed spawn is NOT a pass. `spawnSync` returns status null when the
// process could never start, and `null ?? 0` quietly turns that into success --
// the exact bug that made the machine's dispatcher unable to reach this repo's
// hook, reproduced here one file later if left unguarded.
function runOrFail(script, label) {
  const res = spawnSync(process.execPath, [script, ...args], {
    input: stdin,
    stdio: ["pipe", "inherit", "inherit"],
  });
  if (res.error || res.status === null) {
    console.error(
      `\n[pre-push] o portao "${label}" nao pode ser executado ` +
        `(${res.error ? res.error.code : "sem status"}).\n` +
        "  Um portao que nao roda nao aprova nada. Corrija, ou use --no-verify\n" +
        "  assumindo o risco conscientemente."
    );
    process.exit(1);
  }
  return res.status;
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
  const status = runOrFail(globalDispatch, "global da maquina");
  if (status) process.exit(status);
}

// 2. This repo's gate: the contract tests CI structurally cannot run.
const own = join(__dirname, "..", "scripts", "hooks", "pre-push.cjs");
if (!existsSync(own)) {
  console.error(
    `\n[pre-push] o portao do repositorio nao foi encontrado em ${own}.\n` +
      "  Sem ele os contract tests nao rodam em lugar nenhum -- o CI nao tem\n" +
      "  como roda-los. Restaure o arquivo ou use --no-verify."
  );
  process.exit(1);
}
process.exit(runOrFail(own, "do repositorio"));
