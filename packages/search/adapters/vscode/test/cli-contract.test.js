"use strict";

// Contract guard: the CLI-version pin in package.json, the COMPATIBLE_CLI
// bounds in extension.js, and the real devin-search version must agree.
// The extension probes `devin-search --version` once per session — the
// pin exists so a too-old/too-new CLI is named, not rendered as garbage.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const ADAPTER = path.join(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(ADAPTER, "package.json"), "utf8"));
const src = fs.readFileSync(path.join(ADAPTER, "extension.js"), "utf8");

// packages/search/pyproject.toml is the source of truth for the CLI version.
const pyproject = fs.readFileSync(
  path.join(ADAPTER, "..", "..", "pyproject.toml"),
  "utf8",
);
const realVersion = /^version\s*=\s*"([^"]+)"/m.exec(pyproject)[1];

function parseRange(range) {
  const m = /^>=(\d+)\.(\d+)(?:\.(\d+))?,<(\d+)\.(\d+)(?:\.(\d+))?$/.exec(range);
  assert.ok(m, `pin "${range}" must be a ">=x.y[.z],<x.y[.z]" range`);
  return {
    min: [Number(m[1]), Number(m[2]), Number(m[3] || 0)],
    maxExclusive: [Number(m[4]), Number(m[5]), Number(m[6] || 0)],
  };
}

test("package.json pins the devin-search CLI range", () => {
  assert.ok(pkg["devin-search-cli"], "missing devin-search-cli field");
  parseRange(pkg["devin-search-cli"]);
});

test("COMPATIBLE_CLI matches the package.json pin", () => {
  const { min, maxExclusive } = parseRange(pkg["devin-search-cli"]);
  const m = /COMPATIBLE_CLI\s*=\s*\{\s*min:\s*\[(\d+),\s*(\d+),\s*(\d+)\],\s*maxExclusive:\s*\[(\d+),\s*(\d+),\s*(\d+)\]/.exec(src);
  assert.ok(m, "COMPATIBLE_CLI bounds not found in extension.js");
  assert.deepStrictEqual(
    [Number(m[1]), Number(m[2]), Number(m[3])], min);
  assert.deepStrictEqual(
    [Number(m[4]), Number(m[5]), Number(m[6])], maxExclusive);
});

test("the real devin-search version falls inside the pinned range", () => {
  const { min, maxExclusive } = parseRange(pkg["devin-search-cli"]);
  const ver = realVersion.split(".").map(Number);
  const cmp = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
  assert.ok(
    cmp(ver, min) >= 0 && cmp(ver, maxExclusive) < 0,
    `devin-search ${realVersion} outside ${pkg["devin-search-cli"]}`,
  );
});

test("a too-old CLI (argparse rejects new flags) maps to an upgrade hint", () => {
  assert.ok(
    /unrecognized arguments|invalid choice/.test(src) &&
      src.includes("uv tool upgrade devin-search"),
    "extension.js must map argparse rejections to an upgrade message",
  );
});

test("the CLI self-reports its version inside the JSON payloads", () => {
  // cli.py must emit "version" in the query and misses payloads so future
  // drift is a precise version comparison, not a missing-field failure.
  const cli = fs.readFileSync(
    path.join(ADAPTER, "..", "..", "src", "devin_search", "cli.py"),
    "utf8",
  );
  const occurrences = (cli.match(/"version":\s*__version__/g) || []).length;
  assert.ok(
    occurrences >= 2,
    `cli.py must emit "version": __version__ in query and misses payloads ` +
      `(found ${occurrences})`,
  );
});
