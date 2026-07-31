#!/usr/bin/env node
// Pre-push gate: run the checks CI structurally cannot.
//
// The contract tests that exercise the real Copilot CLI are skipped unless
// RUN_COPILOT_CONTRACT=1, and a hosted runner has no CLI, no subscription and
// no mcp-bridge config -- so they never run there and never will. A green CI on
// this repository is therefore silent about the one class of failure that has
// shipped repeatedly here: a permission that looks granted and is not.
//
// This runs on the machine that HAS all of it, at the only moment that is both
// automatic and cheap: just before the work leaves the laptop.
//
// It is a gate, not a report. An earlier version of the boot canary printed a
// warning and started anyway; that lesson cost three repeats.

const { spawnSync } = require("node:child_process");
const { existsSync } = require("node:fs");
const { join } = require("node:path");

const repo = join(__dirname, "..", "..");
const windows = join(repo, ".venv", "Scripts", "python.exe");
const posix = join(repo, ".venv", "bin", "python");
const interpreter = existsSync(windows) ? windows : posix;

if (!existsSync(interpreter)) {
  console.error("pre-push: venv nao encontrado, pulando os contract tests.");
  process.exit(0);
}

// Only worth spending quota when the conductor layer itself changed. A docs
// commit should not cost a minute and real tokens.
const changed = spawnSync("git", ["diff", "--name-only", "@{u}...HEAD"], {
  encoding: "utf8",
  cwd: repo,
});
const files = (changed.stdout || "").split("\n");
const touchesLayer = files.some((f) =>
  /src\/openjarvis\/(conductor|tools\/(copilot_ide|copilot_sessions|mcp_bridge_config)|agents\/copilot_cli|speech\/vox_engine)|tests\/conductor\/test_contract\.py/.test(
    f
  )
);

// JARVIS_CONTRACT=1 runs them regardless. Useful to check the current working
// tree by hand -- the diff above only sees COMMITS, which is right for a
// pre-push hook and useless while still editing.
const forced = process.env.JARVIS_CONTRACT === "1";

if (!touchesLayer && !forced) {
  console.log("pre-push: a camada do Jarvis nao mudou, contract tests dispensados.");
  process.exit(0);
}

console.log("pre-push: rodando os contract tests contra o CLI real...");
const result = spawnSync(
  interpreter,
  [
    "-m",
    "pytest",
    "tests/conductor/test_contract.py",
    "-q",
    "--no-header",
    "--tb=short",
    "-p",
    "no:cacheprovider",
    // A skip is not a pass. pytest exits 0 when everything is skipped, so a
    // gate that only reads the exit code reports "checked" having checked
    // nothing -- measured here, on the first run of this very hook. This makes
    // pytest itself refuse a run where nothing executed.
    "-p",
    "no:randomly",
    "--strict-markers",
  ],
  {
    cwd: repo,
    stdio: ["inherit", "pipe", "inherit"],
    encoding: "utf8",
    env: { ...process.env, RUN_COPILOT_CONTRACT: "1", PYTHONIOENCODING: "utf-8" },
  }
);

const out = result.stdout || "";
process.stdout.write(out);

if (result.status !== 0) {
  console.error(
    "\npre-push: os contract tests falharam. O que quebrou so aparece contra o " +
      "CLI real, e o CI nao consegue ve-lo. Corrija antes de empurrar, ou use " +
      "--no-verify se souber exatamente por que."
  );
  process.exit(1);
}

if (/\bskipped\b/.test(out) && !/\d+ passed/.test(out)) {
  console.error(
    "\npre-push: os contract tests foram TODOS pulados -- nada foi verificado.\n" +
      "  Um pulo nao e uma aprovacao: sem token com assinatura ou sem o CLI no\n" +
      "  PATH, este portao nao tem como provar nada e nao vai fingir que provou.\n" +
      "  Rode `copilot login`, ou empurre com --no-verify assumindo o risco."
  );
  process.exit(1);
}

console.log("pre-push: contrato com o CLI real conferido.");
