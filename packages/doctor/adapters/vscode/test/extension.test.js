"use strict";

// Minimal vscode stub injected before requiring the extension — enough for
// the pure rendering paths to run outside the editor.
const cpCalls = []; // { exe, args, opts } — every spawn the extension attempts
const cpStub = {
  execFile: (exe, args, opts, cb) => {
    cpCalls.push({ exe, args, opts });
    cb(null, '{"overall":"PASS","version":"0.2.0","summary":{"pass":1}}', "");
  },
};
let settings = {};

const vscodeStub = {
  ThemeColor: class {
    constructor(id) {
      this.id = id;
    }
  },
  StatusBarAlignment: { Left: 1, Right: 2 },
  window: {
    createStatusBarItem: () => ({ show() {}, hide() {} }),
    createOutputChannel: () => ({ appendLine() {}, clear() {}, show() {} }),
    showWarningMessage: () => Promise.resolve(),
    showErrorMessage: () => Promise.resolve(),
    showTextDocument: () => Promise.resolve({ edit: async () => true }),
  },
  workspace: {
    isTrusted: true,
    workspaceFolders: [{ uri: { fsPath: "/repo" } }],
    getConfiguration: () => ({
      get: (k, d) =>
        k === "executable"
          ? process.env.DOCTOR_VSCODE_TEST_EXE || "devin-doctor"
          : k in settings
            ? settings[k]
            : d,
    }),
    openTextDocument: () => Promise.resolve({}),
  },
  commands: { registerCommand: () => ({ dispose() {} }) },
};

const Module = require("module");
const origRequire = Module.prototype.require;
Module.prototype.require = function (id) {
  if (id === "vscode") return vscodeStub;
  if (id === "child_process") return cpStub;
  return origRequire.apply(this, arguments);
};

const ext = require("../extension.js");
const { __internals__ } = ext;
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

test("severityBadge maps CLI overall values case-insensitively", () => {
  const badge = __internals__.severityBadge;
  assert.strictEqual(badge("PASS").text, "$(pass) doctor: PASS");
  assert.strictEqual(badge("warn").text, "$(warning) doctor: WARN");
  assert.strictEqual(badge("FAIL").text, "$(error) doctor: FAIL");
  assert.strictEqual(badge("FAIL").bg.id, "statusBarItem.errorBackground");
  assert.strictEqual(badge("warn").bg.id, "statusBarItem.warningBackground");
  assert.strictEqual(badge("bogus").text, "$(question) doctor: ?");
  assert.strictEqual(badge(undefined).text, "$(question) doctor: ?");
});

test("runDoctor rejects with an actionable error when the CLI is absent", async () => {
  process.env.DOCTOR_VSCODE_TEST_EXE = "devin-doctor-definitely-missing-9f3b";
  try {
    await assert.rejects(
      __internals__.runDoctor(["check", "--json"]),
      /not found on PATH/,
    );
  } finally {
    delete process.env.DOCTOR_VSCODE_TEST_EXE;
  }
});

test("manifest declares limited untrusted-workspace support", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
  );
  const uw = manifest.capabilities.untrustedWorkspaces;
  assert.strictEqual(uw.supported, "limited");
  assert.deepStrictEqual(uw.restrictedConfigurations.sort(), [
    "devinDoctor.checkOnStartup",
    "devinDoctor.executable",
    "devinDoctor.extraArgs",
  ]);
});

test("checkOnStartup never spawns in an untrusted workspace", async () => {
  settings = { checkOnStartup: true };
  // process.execPath resolves on every platform (/bin/true is POSIX-only).
  process.env.DOCTOR_VSCODE_TEST_EXE = process.execPath;
  vscodeStub.workspace.isTrusted = false;
  cpCalls.length = 0;
  ext.activate({ subscriptions: [] });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(cpCalls.length, 0);
  vscodeStub.workspace.isTrusted = true;
  ext.activate({ subscriptions: [] });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(cpCalls.length, 1);
  delete process.env.DOCTOR_VSCODE_TEST_EXE;
  settings = {};
});
