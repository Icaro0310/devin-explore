"use strict";

/*
 * safe-executable.cjs — CANONICAL COPY lives in devin-powerups
 * (adapters/shared/safe-executable.cjs). Byte-identical copies ship in
 * each CLI-wrapper VS Code adapter; the config-restricted-check sensor
 * enforces the lockstep (a drifted copy fails CI). Edit only the
 * canonical file, then re-fan out.
 *
 * Why this exists: on Windows, CreateProcess searches the current
 * directory BEFORE PATH — an adapter that runs
 *     execFile("devin-x", args, { cwd: workspaceDir() })
 * executes a binary planted inside an untrusted repo. `where.exe`
 * searches cwd first too, so resolving through `where` re-opens the
 * same hole. This module walks PATH manually (cwd is never searched),
 * returns absolute spawn targets, and in untrusted workspaces rejects
 * every candidate that resolves inside the workspace. With an
 * absolute path in hand, `cwd: workspaceDir()` on the final spawn is
 * inert — the process search order only applies to bare names.
 */

const fs = require("fs");
const path = require("path");

const isWindows = process.platform === "win32";

function pathExts() {
  return (process.env.PATHEXT || ".COM;.EXE;.BAT;.CMD")
    .split(";")
    .map((e) => e.trim())
    .filter(Boolean);
}

// true when `child` is `parent` itself or lives under it — across
// case-insensitive Windows paths and POSIX.
function isInside(child, parent) {
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

function executableFile(p) {
  let stat;
  try {
    stat = fs.statSync(p);
  } catch {
    return false;
  }
  if (!stat.isFile()) return false;
  if (!isWindows) {
    try {
      fs.accessSync(p, fs.constants.X_OK);
    } catch {
      return false;
    }
  }
  return true;
}

// Candidate absolute paths for `exe`, in search order — never cwd.
function* candidates(exe) {
  if (/[\\/]/.test(exe)) {
    yield path.resolve(exe);
    return;
  }
  const exts = isWindows ? pathExts() : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    if (isWindows && path.extname(exe)) {
      yield path.join(dir, exe);
      continue;
    }
    for (const ext of exts) yield path.join(dir, exe + ext);
  }
}

// An npm `.cmd`/`.bat` shim cannot be spawned directly by execFile.
// The standard npm shim body references the real entry as
// "%~dp0\<relative>.js"; resolve it against the shim's directory.
// Also try the two layouts a package install produces (global prefix
// and project node_modules/.bin) when npmPackage is known.
function resolveShimEntry(shimPath, npmPackage) {
  const dir = path.dirname(shimPath);
  try {
    const body = fs.readFileSync(shimPath, "utf8");
    const m = /"%~dp0[\\/]?([^"\r\n]+?\.js)"/i.exec(body);
    if (m) {
      const entry = path.join(dir, m[1]);
      if (fs.existsSync(entry)) return entry;
    }
  } catch {
    // unreadable shim: fall through to layout guesses
  }
  if (npmPackage) {
    for (const rel of [
      path.join("node_modules", npmPackage, "bin"),
      path.join("..", npmPackage, "bin"),
    ]) {
      const binDir = path.resolve(dir, rel);
      try {
        for (const name of fs.readdirSync(binDir)) {
          if (/\.js$/i.test(name)) return path.join(binDir, name);
        }
      } catch {
        // layout absent — try the next
      }
    }
  }
  return null;
}

/**
 * Resolve `exe` to a spawn target safe under Restricted Mode.
 *
 * @param {string} exe                    executable name or path (from
 *                                        user-level config)
 * @param {object}  [opts]
 * @param {string|null} [opts.workspaceDir]  open workspace folder; when
 *                                        `trusted` is false, candidates
 *                                        resolving inside it are rejected
 * @param {boolean} [opts.trusted]        `vscode.workspace.isTrusted` —
 *                                        trusted workspaces may point at
 *                                        repo-local binaries on purpose
 * @param {string|null} [opts.npmPackage] npm package name, for `.cmd`
 *                                        shim entry resolution on Windows
 * @returns {{file:string,argv:string[]}|null} spawn target; `file` is
 *          absolute (or `process.execPath` for a resolved `.js` entry).
 *          `null` when nothing safe resolves.
 */
function resolveSafeExecutable(exe, opts = {}) {
  if (!exe || typeof exe !== "string") return null;
  const trusted = opts.trusted !== false;
  const ws = opts.workspaceDir ? path.resolve(opts.workspaceDir) : null;

  for (const cand of candidates(exe.trim())) {
    if (!executableFile(cand)) continue;
    const abs = path.resolve(cand);
    if (!trusted && ws && isInside(abs, ws)) continue;
    if (isWindows && /\.(cmd|bat)$/i.test(abs)) {
      const entry = resolveShimEntry(abs, opts.npmPackage || null);
      if (entry) return { file: process.execPath, argv: [entry] };
      continue; // unspawnable shim — try the next candidate
    }
    return { file: abs, argv: [] };
  }
  return null;
}

module.exports = { resolveSafeExecutable, isInside };
