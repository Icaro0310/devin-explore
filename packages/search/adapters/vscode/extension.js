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

// Same bounds as the "devin-search-cli" pin in package.json —
// test/cli-contract.test.js asserts the pin, these bounds and the real
// CLI version agree.
const COMPATIBLE_CLI = { min: [0, 1, 0], maxExclusive: [0, 2, 0] };
const CLI_RANGE = ">=0.1,<0.2";

function parseVersion(version) {
  const m = /^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]*)?$/.exec(
    String(version || "").trim(),
  );
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

const cmp3 = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
// "compatible" | "too-old" | "too-new" | "unknown"
function cliCompatStatus(version) {
  const v = parseVersion(version);
  if (!v) return "unknown";
  if (cmp3(v, COMPATIBLE_CLI.min) < 0) return "too-old";
  if (cmp3(v, COMPATIBLE_CLI.maxExclusive) >= 0) return "too-new";
  return "compatible";
}

// CLIs ≥0.1.2 self-report "version" inside the JSON payload (same probe
// doctor uses); older ones omit it — an absent field is "unknown", not
// drift. Shape validation is the loud guard; this is the precise one.

// Once-per-session `devin-search --version` probe (best-effort). An
// argparse rejection ("unrecognized arguments") is ambiguous — it can
// mean a too-old CLI missing a built-in flag OR a typo in the user's
// devinSearch.extraArgs. The probe plus the flag/setting comparison
// below decides which; the cached result is reused for the session.
// Cached as a promise so concurrent searches share one probe (a second
// caller must not read a null placeholder while the first is still
// awaiting). The cache is keyed by devinSearch.executable — changing the
// configured binary re-probes instead of reusing a stale verdict.
let versionProbe;
let probedExecutable;
function probeVersion() {
  const exe = config().executable;
  if (versionProbe && probedExecutable === exe) return versionProbe;
  probedExecutable = exe;
  versionProbe = (async () => {
    try {
      const { stdout } = await deps.runCli(["--version"], { includeExtraArgs: false });
      const m = /(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]*)?)/.exec(stdout || "");
      return m ? m[1] : null;
    } catch {
      return null; /* probe is best-effort — falls back to the flag check */
    }
  })();
  return versionProbe;
}

function compatSuffix(compat, version) {
  return compat === "too-old" || compat === "too-new"
    ? ` — CLI ${version} outside ${CLI_RANGE}` : "";
}

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
  // opts.includeExtraArgs:false skips devinSearch.extraArgs — used by the
  // --version probe so a bad setting cannot break the probe itself.
  runCli: (args, opts = {}) => {
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
      cp.execFile(spec.file,
        spec.argv.concat(args, ...(opts.includeExtraArgs === false ? [] : extraArgs)),
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
    const args = [
      "query", term.trim(), "--json", "--no-log", "--limit", String(config().limit),
    ];
    const { code, stdout, stderr } = await deps.runCli(args);
    let payload;
    try {
      payload = JSON.parse(stdout);
    } catch {
      output.appendLine(stdout || "");
      output.appendLine(stderr || "");
      // argparse rejects flags it does not know — but that is ambiguous:
      // a too-old CLI missing a built-in flag, or a bad entry in
      // devinSearch.extraArgs. Probe --version once per session and
      // compare the rejected flags against the setting before blaming
      // the CLI.
      if (/unrecognized arguments|invalid choice/.test(stderr || "")) {
        const rejected = (stderr.match(/unrecognized arguments:\s*(.+)/) || [])[1]
          ?.trim().split(/\s+/).filter((t) => t.startsWith("-")) || [];
        const userFlags = new Set(config().extraArgs.filter((t) => t.startsWith("-")));
        // A rejected flag is the CLI's fault only if THIS call sent it;
        // flags the extension never passes (--version, audit-only
        // options) in a rejection must have come from extraArgs.
        const sentFlags = new Set(args.filter((t) => t.startsWith("-")));
        const onlySettings = rejected.length > 0 &&
          rejected.every((t) => userFlags.has(t) && !sentFlags.has(t));
        const probed = await probeVersion();
        const compat = probed ? cliCompatStatus(probed) : "unknown";
        if (onlySettings || compat === "compatible") {
          // Blame the setting without lying about the CLI: when the probe
          // reports a version outside the range, say so instead of
          // claiming compatibility.
          const cliNote = !probed
            ? "the CLI version could not be probed"
            : compat === "compatible"
              ? `the installed CLI ${probed} is inside ${CLI_RANGE}`
              : `the installed CLI ${probed} is also outside ${CLI_RANGE} — upgrade separately`;
          throw new Error(
            `devinSearch.extraArgs contains unsupported option(s): ${rejected.join(", ")} — ` +
            `fix the setting (${cliNote})`);
        }
        const found = probed ? `, found ${probed}` : "";
        const hint = probed
          ? "upgrade: uv tool upgrade devin-search"
          : "upgrade: uv tool upgrade devin-search — or check devinSearch.extraArgs";
        throw new Error(`devin-search CLI is too old for this extension ` +
          `(requires ${CLI_RANGE}${found}); the installed CLI rejected an argument — ${hint}`);
      }
      throw new Error(`devin-search query returned no JSON (exit ${code}) — ` +
        "run `devin-search index` first? See output channel.");
    }
    const shape = validateQueryPayload(payload);
    if (!shape.ok) {
      throw new Error(`devin-search JSON missing ${shape.missing.join(", ")} — ` +
        `extension/CLI drift (compatible: devin-search ${CLI_RANGE})`);
    }
    const compat = cliCompatStatus(payload.version);
    if (seq === searchSeq) {
      lastTerm = term.trim();
      statusBar.text = `$(search) search: ${payload.hits.length}`;
      statusBar.tooltip = `devin-search — "${lastTerm}": ${payload.hits.length} hit(s)` +
        compatSuffix(compat, payload.version);
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
      const compat = cliCompatStatus(s.version);
      if (compat === "too-old" || compat === "too-new") {
        output.appendLine(`CLI ${s.version} outside ${CLI_RANGE} — ` +
          (compat === "too-old" ? "upgrade: uv tool upgrade devin-search"
            : "downgrade the CLI or update the extension"));
      }
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
  COMPATIBLE_CLI, CLI_RANGE, parseVersion, cliCompatStatus, compatSuffix,
  probeVersion, resetProbe: () => { versionProbe = undefined; probedExecutable = undefined; },
};
