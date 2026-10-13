"use strict";

const vscode = require("vscode");
const cp = require("child_process");

let statusBar;
let output;
// Monotonic counter — a slow check finishing late must not overwrite a
// newer verdict already rendered on the status bar.
let checkSeq = 0;

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
  statusBar.text = badge.text;
  statusBar.backgroundColor = badge.bg;
  const s = payload.summary || {};
  statusBar.tooltip =
    `devin-doctor ${payload.version || ""} — ` +
    `${s.pass || 0} pass / ${s.warn || 0} warn / ${s.fail || 0} fail`;
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
    if (seq === checkSeq) {
      renderStatus(payload);
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
module.exports.__internals__ = { severityBadge };
