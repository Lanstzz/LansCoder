#!/usr/bin/env node

import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { buildSessionReport, validateSessionConfiguration } from "./session.mjs";

const REQUIRED_FILES = [
  "AGENTS.md",
  ".ai-team/PROJECT.md",
  ".ai-team/TASK.md",
  ".ai-team/SKILL.md",
  ".ai-team/session.mjs",
];

const REQUIRED_SECTIONS = [
  "Goal",
  "Acceptance scenarios",
  "Invariants",
  "Decisions",
  "Completed",
  "Pending",
  "Next step",
  "Verification",
  "Handoff note",
];

const VALID_STATES = new Set(["planning", "active", "handoff", "blocked", "done"]);

function parseArgs(argv) {
  const options = { root: process.cwd(), base: null, json: false };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--root") options.root = resolve(argv[++index]);
    else if (value === "--base") options.base = argv[++index];
    else if (value === "--json") options.json = true;
    else throw new Error(`Unknown argument: ${value}`);
  }
  return options;
}

function field(markdown, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return markdown.match(new RegExp("^- " + escaped + ": `([^`]+)`$", "m"))?.[1]?.trim() ?? null;
}

function section(markdown, title) {
  const escaped = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return markdown.match(new RegExp(`^## ${escaped}\\s*\\n([\\s\\S]*?)(?=^## |(?![\\s\\S]))`, "m"))?.[1]?.trim() ?? "";
}

function git(root, args) {
  const result = spawnSync("git", args, { cwd: root, encoding: "utf8", windowsHide: true });
  return result.status === 0 ? result.stdout.trim() : null;
}

function isCollaborationFile(path) {
  const normalized = path.replaceAll("\\", "/");
  return (
    normalized === "AGENTS.md" ||
    normalized.startsWith(".ai-team/") ||
    normalized.startsWith(".github/PULL_REQUEST_TEMPLATE/") ||
    normalized === ".github/workflows/repo-task-sync.yml"
  );
}

export function validateRepository({ root = process.cwd(), base = null } = {}) {
  const absoluteRoot = resolve(root);
  const errors = [];
  for (const path of REQUIRED_FILES) {
    if (!existsSync(resolve(absoluteRoot, path))) errors.push(`Missing required file: ${path}`);
  }

  const taskPath = resolve(absoluteRoot, ".ai-team/TASK.md");
  const task = existsSync(taskPath) ? readFileSync(taskPath, "utf8").replaceAll("\r\n", "\n") : "";
  const agentsPath = resolve(absoluteRoot, "AGENTS.md");
  const agents = existsSync(agentsPath) ? readFileSync(agentsPath, "utf8") : "";
  if (agents && !agents.includes("<!-- repo-task-sync:start -->")) {
    errors.push("AGENTS.md does not contain the repo-task-sync entry marker");
  }
  const metadata = {
    id: field(task, "ID"),
    title: field(task, "Title"),
    status: field(task, "Status"),
    owner: field(task, "Owner"),
    nextOwner: field(task, "Next owner"),
  };

  for (const [name, value] of Object.entries(metadata)) {
    if (!value) errors.push(`TASK.md is missing metadata: ${name}`);
  }
  if (metadata.status && !VALID_STATES.has(metadata.status)) {
    errors.push(`TASK.md has invalid Status: ${metadata.status}`);
  }
  for (const title of REQUIRED_SECTIONS) {
    if (!section(task, title)) errors.push(`TASK.md section is missing or empty: ${title}`);
  }
  if (["active", "handoff", "blocked", "done"].includes(metadata.status) && metadata.owner === "unassigned") {
    errors.push(`TASK.md Status ${metadata.status} requires an assigned Owner`);
  }
  if (metadata.status === "handoff" && (!metadata.nextOwner || metadata.nextOwner === "unassigned")) {
    errors.push("TASK.md Status handoff requires an assigned Next owner");
  }

  const acceptance = section(task, "Acceptance scenarios");
  const acceptanceItems = acceptance.match(/^- \[[ xX]\] .+$/gm) ?? [];
  const accepted = acceptanceItems.filter((item) => /^- \[[xX]\]/.test(item)).length;
  const verification = section(task, "Verification");
  const verificationItems = verification.match(/^- \[[ xX]\] .+$/gm) ?? [];
  const verified = verificationItems.filter((item) => /^- \[[xX]\]/.test(item)).length;
  if (acceptanceItems.length === 0) errors.push("TASK.md requires at least one acceptance checkbox");
  if (metadata.status === "done" && accepted !== acceptanceItems.length) {
    errors.push("TASK.md Status done requires every acceptance scenario to be checked");
  }
  if (metadata.status === "done" && (verificationItems.length === 0 || verified !== verificationItems.length)) {
    errors.push("TASK.md Status done requires every verification item to be checked");
  }

  const sessionValidation = validateSessionConfiguration({ root: absoluteRoot });
  errors.push(...sessionValidation.errors);
  const sessions = buildSessionReport({ root: absoluteRoot });

  const gitProgress = {
    available: false,
    base,
    commits: null,
    changedFiles: null,
    additions: null,
    deletions: null,
    files: [],
  };

  if (base) {
    const range = `${base}..HEAD`;
    const ancestor = spawnSync("git", ["merge-base", "--is-ancestor", base, "HEAD"], {
      cwd: absoluteRoot,
      encoding: "utf8",
      windowsHide: true,
    });
    const changed = git(absoluteRoot, ["diff", "--name-only", base, "--"]);
    if (ancestor.status !== 0 || changed === null) {
      errors.push(`Git base is unavailable or not an ancestor: ${base}`);
    } else {
      const trackedFiles = changed ? changed.split(/\r?\n/).filter(Boolean) : [];
      const untracked = git(absoluteRoot, ["ls-files", "--others", "--exclude-standard"]);
      const untrackedFiles = untracked ? untracked.split(/\r?\n/).filter(Boolean) : [];
      const files = [...new Set([...trackedFiles, ...untrackedFiles])].sort();
      const nonCollaborationFiles = files.filter((path) => !isCollaborationFile(path));
      if (nonCollaborationFiles.length > 0 && !files.includes(".ai-team/TASK.md")) {
        errors.push("Code or product files changed without updating .ai-team/TASK.md in the same PR");
      }

      const numstat = git(absoluteRoot, ["diff", "--numstat", base, "--"]) ?? "";
      let additions = 0;
      let deletions = 0;
      for (const line of numstat.split(/\r?\n/).filter(Boolean)) {
        const [added, deleted] = line.split("\t");
        if (/^\d+$/.test(added)) additions += Number(added);
        if (/^\d+$/.test(deleted)) deletions += Number(deleted);
      }
      gitProgress.available = true;
      gitProgress.commits = Number(git(absoluteRoot, ["rev-list", "--count", range]) ?? 0);
      gitProgress.changedFiles = files.length;
      gitProgress.additions = additions;
      gitProgress.deletions = deletions;
      gitProgress.files = files;
    }
  }

  return {
    valid: errors.length === 0,
    task: {
      ...metadata,
      acceptance: {
        completed: accepted,
        total: acceptanceItems.length,
        percent: acceptanceItems.length ? Math.round((accepted / acceptanceItems.length) * 100) : null,
      },
      verification: {
        completed: verified,
        total: verificationItems.length,
      },
    },
    git: gitProgress,
    sessions,
    errors,
  };
}

function printHuman(result) {
  const progress = result.task.acceptance;
  process.stdout.write(
    [
      `Task: ${result.task.id ?? "unavailable"} — ${result.task.title ?? "unavailable"}`,
      `State: ${result.task.status ?? "unavailable"}; owner: ${result.task.owner ?? "unavailable"}; next: ${result.task.nextOwner ?? "unavailable"}`,
      `Functional progress: ${progress.completed}/${progress.total}${progress.percent === null ? "" : ` (${progress.percent}%)`}`,
      result.git.available
        ? `Code progress from ${result.git.base}: ${result.git.commits} commits, ${result.git.changedFiles} files, +${result.git.additions}/-${result.git.deletions}`
        : "Code progress: provide --base <target-branch-or-sha> to compare Git changes",
      result.sessions.enabled
        ? `Private sessions: ${result.sessions.totals.sessions}; closed: ${result.sessions.totals.closed}; token coverage: ${result.sessions.totals.tokenCoverage.reported}/${result.sessions.totals.tokenCoverage.total}`
        : "Private sessions: disabled",
      result.valid ? "Result: valid" : `Result: blocked\n- ${result.errors.join("\n- ")}`,
    ].join("\n") + "\n",
  );
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : null;
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    const options = parseArgs(process.argv.slice(2));
    const result = validateRepository(options);
    if (options.json) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    else printHuman(result);
    if (!result.valid) process.exitCode = 1;
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  }
}
