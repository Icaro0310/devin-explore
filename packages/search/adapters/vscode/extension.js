"use strict";

const vscode = require("vscode");
const cp = require("child_process");
// Shared Restricted-Mode-safe executable resolver — byte-identical copy of
// devin-powerups/adapters/shared/safe-executable.cjs (lockstep enforced).
// On Windows, CreateProcess searches cwd before PATH: spawn only the
// absolute path this returns.
const { resolveSafeExecutable } = require("./safe-executable.cjs");

let statusBar;
let output;
// Monotonic counter — a slow query finishing late must not overwrite a
// newer result already rendered on the status bar (anti-race guard).
let searchSeq = 0;
let lastTerm = null;

// devin-search LIMIT maps to SQLite's: -1 means "unlimited" and can blow
// the 8 MiB output buffer, so the setting is clamped to a sane range.
function clampLimit(n) {
  const v = Number(n);
  return Number.isFinite(v) ? Math.min(500, Math.max(1, Math.trunc(v))) : 20;
}

function config() {
  const c = vscode.workspace.getConfiguration("devinSearch");
  return { executable: c.get("executable", "devin-search"),
    extraArgs: c.get("extraArgs", []), limit: clampLimit(c.get("limit", 20)) };
}

// Every open workspace folder — used only to bound
// resolveSafeExecutable's inside-workspace rejection, not as spawn cwd.
// Multi-root: a PATH candidate living in ANY folder is workspace code.
function workspaceDirs() {
  const folders = vscode.workspace.workspaceFolders;
  return folders ? folders.map((f) => f.uri.fsPath) : [];
}

// Injectable for tests. Non-zero exits are results, not errors:
// query exits 1 on zero hits, 2 on usage/store problems.
const deps = {
  runCli: (args) => {
    const { executable, extraArgs } = config();
    const spec = resolveSafeExecutable(executable, {
      workspaceDirs: workspaceDirs(),
      trusted: vscode.workspace.isTrusted,
      npmPackage: null,
    });
    return new Promise((resolve, reject) => {
      if (!spec) {
        return reject(new Error(
          "devin-search not found on PATH. Install it " +
            "(uv tool install devin-search) or set devinSearch.executable."));
      }
      cp.execFile(spec.file, spec.argv.concat(args, extraArgs),
        { timeout: 120000, maxBuffer: 8 * 1024 * 1024 },
        (err, stdout, stderr) => {
          if (err && err.killed) {
            return reject(new Error("devin-search timed out after 120s"));
          }
          if (err && err.code === "ENOENT") {
            return reject(new Error(
              "devin-search not found on PATH. Install it " +
                "(uv tool install devin-search) or set devinSearch.executable."));
          }
          resolve({ code: err ? err.code : 0, stdout, stderr });
        });
    });
  },
};

// Contract: `devin-search query --json` → {term, hits:[{session_id, role,
// ts(ms), project, session_title, source, ref, snippet, rank,
// history_note}]}. Drift surfaces loudly, never renders blank.
function validateQueryPayload(payload) {
  const missing = [];
  if (!payload || typeof payload !== "object") return { ok: false, missing: ["<root>"] };
  if (typeof payload.term !== "string") missing.push("term");
  if (!Array.isArray(payload.hits)) {
    missing.push("hits");
  } else {
    payload.hits.forEach((h, i) => {
      for (const k of ["session_id", "role", "snippet"]) {
        if (!h || typeof h[k] !== "string") missing.push(`hits[${i}].${k}`);
      }
      if (h && typeof h.ts !== "number") missing.push(`hits[${i}].ts`);
    });
  }
  return { ok: missing.length === 0, missing };
}

function fmtHit(hit) {
  const when = hit.ts
    ? new Date(hit.ts).toISOString().slice(0, 16).replace("T", " ")
    : "-";
  const tail = String(hit.project || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  return { when, proj: tail[tail.length - 1] || "-",
    sid: hit.session_id.slice(0, 8) };
}

function quickPickItem(h) {
  const f = fmtHit(h);
  return {
    label: `${f.when} · ${f.proj} · ${f.sid}`,
    description: h.role,
    detail: String(h.snippet).replace(/\s+/g, " ").trim().slice(0, 140),
    hit: h,
  };
}

async function openHit(hit) {
  const f = fmtHit(hit);
  const rows = [
    ["when", `${f.when} UTC`], ["role", hit.role], ["session", hit.session_title],
    ["project", hit.project], ["source", hit.source], ["ref", hit.ref], ["history note", hit.history_note],
  ];
  const md =
    `# devin-search hit — ${hit.session_id}\n\n` +
    rows.filter(([, v]) => v).map(([k, v]) => `- **${k}**: ${v}`).join("\n") +
    `\n\n---\n\n${hit.snippet}`;
  const doc = await vscode.workspace.openTextDocument({ language: "markdown", content: md });
  await vscode.window.showTextDocument(doc, { preview: true });
}

async function search() {
  const term = await vscode.window.showInputBox({
    prompt: "Search past Devin sessions",
    value: lastTerm || "",
    placeHolder: "free-text — tokens are ANDed",
  });
  if (term === undefined || term.trim() === "") return null;
  const seq = ++searchSeq;
  statusBar.text = "$(sync~spin) search: running"; statusBar.show();
  try {
    // --no-log keeps the extension strictly read-only: the CLI otherwise
    // appends every query to <index>.queries.jsonl.
    const { code, stdout, stderr } = await deps.runCli([
      "query", term.trim(), "--json", "--no-log", "--limit", String(config().limit),
    ]);
    let payload;
    try {
      payload = JSON.parse(stdout);
    } catch {
      output.appendLine(stdout || "");
      output.appendLine(stderr || "");
      throw new Error(`devin-search query returned no JSON (exit ${code}) — ` +
        "run `devin-search index` first? See output channel.");
    }
    const shape = validateQueryPayload(payload);
    if (!shape.ok) {
      throw new Error(`devin-search JSON missing ${shape.missing.join(", ")} — ` +
        "extension/CLI drift (compatible: devin-search >=0.1,<0.2)");
    }
    if (seq === searchSeq) {
      lastTerm = term.trim();
      statusBar.text = `$(search) search: ${payload.hits.length}`;
      statusBar.tooltip = `devin-search — "${lastTerm}": ${payload.hits.length} hit(s)`;
      if (payload.hits.length === 0) {
        vscode.window.showInformationMessage(`devin-search: no hits for "${lastTerm}"`);
      } else {
        const pick = await vscode.window.showQuickPick(
          payload.hits.map(quickPickItem),
          { placeHolder: `${payload.hits.length} hit(s) for "${lastTerm}"` },
        );
        // Re-check after the await: a newer search may have superseded this pick.
        if (pick && seq === searchSeq) await openHit(pick.hit);
      }
    }
    return payload;
  } catch (e) {
    if (seq === searchSeq) {
      statusBar.text = "$(error) search: unavailable";
      statusBar.tooltip = e.message;
      vscode.window.showErrorMessage(e.message);
    }
    return null;
  }
}

async function showMisses() {
  try {
    const { code, stdout, stderr } = await deps.runCli(["misses", "--json"]);
    output.clear();
    try {
      const s = JSON.parse(stdout);
      output.appendLine(`query log: ${s.log}`);
      output.appendLine(`${s.total_queries} queries · ${s.zero_hit} zero-hit ` +
        `(${Math.round((s.zero_hit_rate || 0) * 100)}%)`);
      Object.entries(s.per_month || {}).sort().forEach(([m, b]) =>
        output.appendLine(`  ${m}: ${b.queries} queries, ${b.zero_hit} zero-hit`));
      (s.top_missed_terms || []).forEach(([t, n]) =>
        output.appendLine(`  ${n}× ${t}`));
    } catch {
      output.appendLine(stdout || stderr);
    }
    output.show();
    if (code !== 0 && !stdout) vscode.window.showErrorMessage(`devin-search misses exited ${code}: ${stderr}`);
  } catch (e) {
    // Spawn failure (e.g. ENOENT) — the message itself carries guidance.
    vscode.window.showErrorMessage(e.message);
  }
}

function activate(context) {
  output = vscode.window.createOutputChannel("devin-search");
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusBar.command = "devin-search.search";
  statusBar.text = "$(search) search"; statusBar.show();
  statusBar.tooltip = "Click to search past Devin sessions";
  context.subscriptions.push(
    output,
    statusBar,
    vscode.commands.registerCommand("devin-search.search", search),
    vscode.commands.registerCommand("devin-search.misses", showMisses),
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
// Test-only surface — internals reachable without the editor.
module.exports.__internals__ = {
  deps, clampLimit, validateQueryPayload, fmtHit, search, showMisses,
};
