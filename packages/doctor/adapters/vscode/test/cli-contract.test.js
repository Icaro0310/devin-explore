"use strict";

// CLI<->extension contract: the JSON emitted by `devin-doctor check --json`
// is a versioned API. These tests pin (a) the compatible CLI range declared
// in package.json against the range enforced in extension.js, and (b) the
// payload shape the extension renders. A CLI that drifts out of shape or
// range must surface as a loud failure here, not a blank badge in the editor.

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
    getConfiguration: () => ({ get: (k, d) => (k === "executable" ? "devin-doctor" : d) }),
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
const path = require("node:path");
const fs = require("node:fs");
const cp = require("node:child_process");

const pkg = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
);

test("package.json devin-doctor-cli range matches COMPATIBLE_CLI", () => {
  const range = pkg["devin-doctor-cli"];
  assert.ok(range, 'package.json must declare "devin-doctor-cli"');
  const m = /^>=(\d+)\.(\d+),<(\d+)\.(\d+)$/.exec(range);
  assert.ok(m, `unparseable range: ${range}`);
  assert.deepStrictEqual(__internals__.COMPATIBLE_CLI.min, [Number(m[1]), Number(m[2])]);
  assert.deepStrictEqual(__internals__.COMPATIBLE_CLI.maxExclusive, [
    Number(m[3]),
    Number(m[4]),
  ]);
});

test("cliCompatStatus classifies versions against the pinned range", () => {
  const { cliCompatStatus } = __internals__;
  assert.strictEqual(cliCompatStatus("0.1.0"), "compatible");
  assert.strictEqual(cliCompatStatus("0.1.9"), "compatible");
  assert.strictEqual(cliCompatStatus("0.0.9"), "too-old");
  assert.strictEqual(cliCompatStatus("0.2.0"), "too-new");
  assert.strictEqual(cliCompatStatus("1.0.0"), "too-new");
  assert.strictEqual(cliCompatStatus(""), "unknown");
  assert.strictEqual(cliCompatStatus("not-a-version"), "unknown");
  assert.strictEqual(cliCompatStatus(undefined), "unknown");
});

const VALID_PAYLOAD = {
  tool: "devin-doctor",
  version: "0.1.0",
  generated_at: "2026-10-11T00:00:00+00:00",
  platform: "linux",
  data_dir: "/tmp/doctor",
  cwd: "/tmp",
  overall: "PASS",
  summary: { pass: 3, warn: 0, fail: 0 },
  checks: { stores: "PASS" },
  findings: [
    { check: "stores", status: "PASS", message: "ok", fix: "" },
    { check: "hooks", status: "WARN", message: "missing", fix: "run x" },
  ],
};

test("validateDoctorPayload accepts the documented contract", () => {
  const { validateDoctorPayload } = __internals__;
  const r = validateDoctorPayload(VALID_PAYLOAD);
  assert.deepStrictEqual(r, { ok: true, missing: [] });
  // fix may legitimately be null/absent — only wrong types drift.
  const noFix = JSON.parse(JSON.stringify(VALID_PAYLOAD));
  delete noFix.findings[0].fix;
  noFix.findings[1].fix = null;
  assert.strictEqual(validateDoctorPayload(noFix).ok, true);
});

test("validateDoctorPayload flags drifted payloads", () => {
  const { validateDoctorPayload } = __internals__;
  const cases = [
    [null, ["<root>"]],
    [{}, ["overall", "version", "summary", "findings"]],
    [{ ...VALID_PAYLOAD, overall: 1 }, ["overall"]],
    [{ ...VALID_PAYLOAD, summary: { pass: 0 } }, ["summary.warn", "summary.fail"]],
    [{ ...VALID_PAYLOAD, findings: {} }, ["findings"]],
    [
      {
        ...VALID_PAYLOAD,
        findings: [{ check: "x", status: "PASS", message: 42, fix: "" }],
      },
      ["findings[0].message"],
    ],
    [
      {
        ...VALID_PAYLOAD,
        findings: [{ check: "x", status: "PASS", message: "m", fix: 7 }],
      },
      ["findings[0].fix"],
    ],
  ];
  for (const [payload, expected] of cases) {
    const r = validateDoctorPayload(payload);
    assert.strictEqual(r.ok, false, JSON.stringify(payload));
    assert.deepStrictEqual(r.missing.sort(), expected.sort());
  }
});

// Live smoke: opt-in only — runs the real CLI when explicitly requested and
// the binary exists. Guards the schema against drift on a dev machine; CI
// relies on the fixture tests plus the Python-side contract test.
test(
  "live CLI emits a payload satisfying the contract",
  { skip: !process.env.DOCTOR_VSCODE_LIVE },
  () => {
    const exe = process.env.DOCTOR_VSCODE_LIVE === "1" ? "devin-doctor" : process.env.DOCTOR_VSCODE_LIVE;
    const out = cp.execFileSync(exe, ["check", "--json"], {
      timeout: 120000,
      maxBuffer: 8 * 1024 * 1024,
      encoding: "utf8",
    });
    const payload = JSON.parse(out);
    const r = __internals__.validateDoctorPayload(payload);
    assert.deepStrictEqual(r, { ok: true, missing: [] });
    assert.strictEqual(__internals__.cliCompatStatus(payload.version), "compatible");
  },
);
