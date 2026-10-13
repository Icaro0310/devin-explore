"use strict";

// Minimal vscode stub injected before requiring the extension — enough for
// the pure rendering paths to run outside the editor.
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
    getConfiguration: () => ({
      get: (k, d) =>
        k === "executable"
          ? process.env.DOCTOR_VSCODE_TEST_EXE || "devin-doctor"
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
  return origRequire.apply(this, arguments);
};

const { __internals__ } = require("../extension.js");
const test = require("node:test");
const assert = require("node:assert");

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
