"use strict";

const vscode = require("vscode");
const cp = require("child_process");

let statusBar;
let output;
// Monotonic counter — a slow check finishing late must not overwrite a
// newer verdict already rendered on the status bar.
let checkSeq = 0;

// The JSON emitted by `devin-doctor check --json` is a versioned contract:
// the extension renders fields that only exist within a known CLI range.
// This range must stay in sync with the "devin-doctor-cli" field in
// package.json — test/cli-contract.test.js asserts it.
const COMPATIBLE_CLI = { min: [0, 1], maxExclusive: [0, 2] };

function parseVersion(version) {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(String(version || "").trim());
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

// "compatible" | "too-old" | "too-new" | "unknown"
function cliCompatStatus(version) {
  const v = parseVersion(version);
  if (!v) return "unknown";
  const [maj, min] = v;
  const [minMaj, minMin] = COMPATIBLE_CLI.min;
  const [maxMaj, maxMin] = COMPATIBLE_CLI.maxExclusive;
  if (maj < minMaj || (maj === minMaj && min < minMin)) return "too-old";
  if (maj > maxMaj || (maj === maxMaj && min >= maxMin)) return "too-new";
  return "compatible";
}

// Fields the extension renders. Returns {ok, missing[]} — a payload that
// fails validation means the CLI and extension have drifted apart, which
// is surfaced to the user instead of silently rendering garbage.
function validateDoctorPayload(payload) {
  const missing = [];
  if (!payload || typeof payload !== "object") return { ok: false, missing: ["<root>"] };
  if (typeof payload.overall !== "string") missing.push("overall");
  if (typeof payload.version !== "string") missing.push("version");
  const s = payload.summary;
  if (!s || typeof s !== "object") {
    missing.push("summary");
  } else {
    for (const k of ["pass", "warn", "fail"]) {
      if (typeof s[k] !== "number") missing.push(`summary.${k}`);
    }
  }
  if (!Array.isArray(payload.findings)) {
    missing.push("findings");
  } else {
    payload.findings.forEach((f, i) => {
      for (const k of ["check", "status", "message"]) {
        if (!f || typeof f[k] !== "string") missing.push(`findings[${i}].${k}`);
      }
      if (f && f.fix !== undefined && f.fix !== null && typeof f.fix !== "string") {
        missing.push(`findings[${i}].fix`);
      }
    });
  }
  return { ok: missing.length === 0, missing };
}

function config() {
  const c = vscode.workspace.getConfiguration("devinDoctor");
  return {
    executable: c.get("executable", "devin-doctor"),
    extraArgs: c.get("extraArgs", []),
  };
}

// Runs the CLI once and resolves {code, stdout, stderr}. Never throws on a
// non-zero exit — doctor signals findings through exit codes (0 ok, 1 warn,
// 2 fail), which are results, not errors.
function runDoctor(args) {
  const { executable, extraArgs } = config();
  return new Promise((resolve, reject) => {
    cp.execFile(
      executable,
      args.concat(extraArgs),
      { timeout: 120000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err && err.killed) {
          reject(new Error(`devin-doctor timed out after 120s`));
          return;
        }
        if (err && err.code === "ENOENT") {
          reject(
            new Error(
              `devin-doctor not found on PATH. Install it (uv tool install devin-doctor) ` +
              `or set devinDoctor.executable.`,
            ),
          );
          return;
        }
        resolve({ code: err ? err.code : 0, stdout, stderr });
      },
    );
  });
}

// The CLI emits uppercase values (Status.PASS = "PASS"); normalize before
// matching so a casing change upstream can never blank the badge.
function severityBadge(overall) {
  switch (String(overall || "").toLowerCase()) {
    case "fail":
      return { text: "$(error) doctor: FAIL", bg: new vscode.ThemeColor("statusBarItem.errorBackground") };
    case "warn":
      return { text: "$(warning) doctor: WARN", bg: new vscode.ThemeColor("statusBarItem.warningBackground") };
    case "pass":
      return { text: "$(pass) doctor: PASS", bg: undefined };
    default:
      return { text: "$(question) doctor: ?", bg: undefined };
  }
}

function renderStatus(payload) {
  const badge = severityBadge(payload.overall);
  const compat = cliCompatStatus(payload.version);
  statusBar.text = compat === "compatible" ? badge.text : `${badge.text} $(warning)`;
  statusBar.backgroundColor = badge.bg;
  const s = payload.summary || {};
  statusBar.tooltip =
    `devin-doctor ${payload.version || "?"} — ` +
    `${s.pass || 0} pass / ${s.warn || 0} warn / ${s.fail || 0} fail` +
    (compat === "compatible"
      ? ""
      : ` — outside compatible range >=${COMPATIBLE_CLI.min.join(".")}.0, <${COMPATIBLE_CLI.maxExclusive.join(".")}.0`);
  statusBar.show();
}

async function check() {
  const seq = ++checkSeq;
  statusBar.text = "$(sync~spin) doctor: running";
  statusBar.show();
  try {
    const { code, stdout, stderr } = await runDoctor(["check", "--json"]);
    let payload;
    try {
      payload = JSON.parse(stdout);
    } catch {
      output.appendLine(stdout);
      output.appendLine(stderr);
      throw new Error("devin-doctor did not return JSON — see output channel");
    }
    const shape = validateDoctorPayload(payload);
    if (!shape.ok) {
      throw new Error(
        `devin-doctor JSON is missing ${shape.missing.join(", ")} — ` +
          `extension/CLI version drift? (compatible: devin-doctor ` +
          `${COMPATIBLE_CLI.min.join(".")}.x, <${COMPATIBLE_CLI.maxExclusive.join(".")}.0)`,
      );
    }
    if (seq === checkSeq) {
      renderStatus(payload);
      const compat = cliCompatStatus(payload.version);
      if (compat !== "compatible") {
        vscode.window.showWarningMessage(
          `devin-doctor ${payload.version} is outside this extension's compatible ` +
            `range (>=${COMPATIBLE_CLI.min.join(".")}.0, <${COMPATIBLE_CLI.maxExclusive.join(".")}.0). ` +
            `Update one of them — the rendered verdict may be untrustworthy.`,
        );
      }
      const failCount = (payload.summary || {}).fail || 0;
      if (failCount > 0) {
        vscode.window.showWarningMessage(
          `devin-doctor: ${failCount} failing check(s). Run "devin-doctor: Open full report" for details.`,
        );
      }
    }
    return payload;
  } catch (e) {
    if (seq === checkSeq) {
      statusBar.text = "$(error) doctor: unavailable";
      statusBar.tooltip = e.message;
      statusBar.show();
      vscode.window.showErrorMessage(e.message);
    }
    return null;
  }
}

async function openReport() {
  const doc = await vscode.workspace.openTextDocument({ language: "markdown" });
  const editor = await vscode.window.showTextDocument(doc, { preview: true });
  const { code, stdout, stderr } = await runDoctor(["report", "--md"]);
  await editor.edit((b) => b.insert(new vscode.Position(0, 0), stdout || stderr));
  if (code !== 0 && !stdout) {
    vscode.window.showErrorMessage(`devin-doctor report exited ${code}: ${stderr}`);
  }
}

async function openPlan() {
  const { code, stdout, stderr } = await runDoctor(["plan"]);
  output.clear();
  output.appendLine(stdout || stderr);
  output.show();
  if (code !== 0 && !stdout) {
    vscode.window.showErrorMessage(`devin-doctor plan exited ${code}: ${stderr}`);
  }
}

function activate(context) {
  output = vscode.window.createOutputChannel("devin-doctor");
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusBar.command = "devin-doctor.check";
  statusBar.text = "$(pulse) doctor";
  statusBar.tooltip = "Click to run devin-doctor health check";
  statusBar.show();

  context.subscriptions.push(
    output,
    statusBar,
    vscode.commands.registerCommand("devin-doctor.check", check),
    vscode.commands.registerCommand("devin-doctor.report", openReport),
    vscode.commands.registerCommand("devin-doctor.plan", openPlan),
  );

  if (vscode.workspace.getConfiguration("devinDoctor").get("checkOnStartup", false)) {
    check();
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
// Test-only surface — pure internals reachable without the editor.
module.exports.__internals__ = {
  severityBadge,
  cliCompatStatus,
  validateDoctorPayload,
  COMPATIBLE_CLI,
};
