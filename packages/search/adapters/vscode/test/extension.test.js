"use strict";

// Minimal vscode stub injected before requiring the extension — enough for
// the pure rendering paths and the seq-guard to run outside the editor.
const statusBarStub = { text: "", tooltip: "", command: "", show() {}, hide() {} };
const outputStub = { lines: [], appendLine(l) { this.lines.push(l); }, clear() { this.lines = []; }, show() {} };

const vscodeStub = {
  ThemeColor: class {
    constructor(id) {
      this.id = id;
    }
  },
  StatusBarAlignment: { Left: 1, Right: 2 },
  window: {
    createStatusBarItem: () => statusBarStub,
    createOutputChannel: () => outputStub,
    showInformationMessage: () => Promise.resolve(),
    showWarningMessage: () => Promise.resolve(),
    showErrorMessage: () => Promise.resolve(),
    showTextDocument: () => Promise.resolve({ edit: async () => true }),
    showQuickPick: () => Promise.resolve(undefined),
    showInputBox: () => Promise.resolve("test-term"),
  },
  workspace: {
    getConfiguration: () => ({
      get: (k, d) =>
        k === "executable"
          ? process.env.SEARCH_VSCODE_TEST_EXE || "devin-search"
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

const ext = require("../extension.js");
const { __internals__ } = ext;
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const fs = require("node:fs");

const HIT = {
  session_id: "abcdef1234567890",
  role: "assistant",
  ts: 1760000000000,
  project: "/home/u/repo",
  session_title: "fix tests",
  source: "sessions.db",
  ref: "msg:1",
  snippet: "ran «pytest» and it passed",
  rank: -1.5,
  history_note: null,
};

test("validateQueryPayload accepts the documented contract", () => {
  const v = __internals__.validateQueryPayload;
  assert.deepStrictEqual(v({ term: "pytest", hits: [HIT] }), { ok: true, missing: [] });
  assert.deepStrictEqual(v({ term: "x", hits: [] }), { ok: true, missing: [] });
  // Optional fields absent is fine.
  const minimal = { session_id: "s", role: "user", ts: 1, snippet: "…" };
  assert.strictEqual(v({ term: "x", hits: [minimal] }).ok, true);
});

test("validateQueryPayload flags drifted payloads", () => {
  const v = __internals__.validateQueryPayload;
  const cases = [
    [null, ["<root>"]],
    [{}, ["term", "hits"]],
    [{ term: "x", hits: "no" }, ["hits"]],
    [{ term: 7, hits: [] }, ["term"]],
    [{ term: "x", hits: [{ ...HIT, session_id: 1 }] }, ["hits[0].session_id"]],
    [{ term: "x", hits: [{ ...HIT, snippet: null }] }, ["hits[0].snippet"]],
    [{ term: "x", hits: [{ ...HIT, ts: "now" }] }, ["hits[0].ts"]],
  ];
  for (const [payload, expected] of cases) {
    const r = v(payload);
    assert.strictEqual(r.ok, false, JSON.stringify(payload));
    assert.deepStrictEqual(r.missing.sort(), expected.sort());
  }
});

test("fmtHit renders when/proj/sid from a contract hit", () => {
  const f = __internals__.fmtHit(HIT);
  assert.strictEqual(f.when, "2025-10-09 08:53");
  assert.strictEqual(f.proj, "repo");
  assert.strictEqual(f.sid, "abcdef12");
  assert.strictEqual(__internals__.fmtHit({ ...HIT, ts: 0 }).when, "-");
  assert.strictEqual(__internals__.fmtHit({ ...HIT, project: "" }).proj, "-");
});

test("overlapping searches: a stale result never overwrites a newer one", async () => {
  ext.activate({ subscriptions: [] });
  const real = __internals__.deps.runCli;
  const deferreds = [];
  __internals__.deps.runCli = (args) =>
    new Promise((resolve) => deferreds.push({ args, resolve }));
  try {
    const p1 = __internals__.search(); // seq=1
    const p2 = __internals__.search(); // seq=2
    // Let both calls pass the awaited input box and reach deps.runCli.
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(deferreds.length, 2);
    // --no-log must be on every query — the read-only contract.
    assert.ok(deferreds.every((d) => d.args.includes("--no-log")));
    // Newer search resolves first and renders.
    deferreds[1].resolve({
      code: 0,
      stdout: JSON.stringify({ term: "b", hits: [HIT, HIT] }),
      stderr: "",
    });
    await p2;
    assert.strictEqual(statusBarStub.text, "$(search) search: 2");
    // The stale search resolves late with a different count — dropped.
    deferreds[0].resolve({
      code: 0,
      stdout: JSON.stringify({ term: "a", hits: [HIT, HIT, HIT, HIT, HIT] }),
      stderr: "",
    });
    const r1 = await p1;
    assert.strictEqual(statusBarStub.text, "$(search) search: 2");
    // The payload is still returned to the caller; only the render is dropped.
    assert.strictEqual(r1.hits.length, 5);
  } finally {
    __internals__.deps.runCli = real;
  }
});

test("deps.runCli rejects with an actionable error when the CLI is absent", async () => {
  process.env.SEARCH_VSCODE_TEST_EXE = "devin-search-definitely-missing-9f3b";
  try {
    await assert.rejects(
      __internals__.deps.runCli(["query", "x", "--json"]),
      /not found on PATH/,
    );
  } finally {
    delete process.env.SEARCH_VSCODE_TEST_EXE;
  }
});

test("package.json devin-search-cli pin covers the package version", () => {
  const pkg = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
  );
  const range = pkg["devin-search-cli"];
  assert.ok(range, 'package.json must declare "devin-search-cli"');
  const m = /^>=(\d+)\.(\d+),<(\d+)\.(\d+)$/.exec(range);
  assert.ok(m, `unparseable range: ${range}`);
  // The pin must cover the real package version (../pyproject.toml).
  const pyproject = fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "pyproject.toml"),
    "utf8",
  );
  const v = /version\s*=\s*"(\d+)\.(\d+)\.\d+"/.exec(pyproject);
  assert.ok(v, "package version not found in pyproject.toml");
  const [maj, min] = [Number(v[1]), Number(v[2])];
  assert.ok(
    (maj > Number(m[1]) || (maj === Number(m[1]) && min >= Number(m[2]))) &&
      (maj < Number(m[3]) || (maj === Number(m[3]) && min < Number(m[4]))),
    `package ${v[0]} outside pinned range ${range}`,
  );
});
