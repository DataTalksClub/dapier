#!/usr/bin/env node
// Local companion server for the workflow designer: reads/writes workflows/*.yaml
// in the dapier repo and commits/pushes to git. Binds to 127.0.0.1 only.
import { execFile } from "node:child_process";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { createReadStream, existsSync, unlinkSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import yaml from "js-yaml";

const execFileAsync = promisify(execFile);

const REPO_ROOT = process.env.DAPIER_ROOT ?? path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WORKFLOWS_DIR = path.join(REPO_ROOT, "workflows");
const DIST_DIR = path.join(import.meta.dirname, "dist");
const PORT = Number(process.env.DESIGNER_PORT ?? 8787);
const HOST = process.env.DESIGNER_HOST ?? "127.0.0.1";
// Alternative to TCP for fully local setups: DESIGNER_UNIX_SOCKET=/run/user/1000/dapier-designer.sock
const UNIX_SOCKET = process.env.DESIGNER_UNIX_SOCKET;
const GIT_TIMEOUT_MS = 60_000;

function gitArgs(args) {
  return { cwd: REPO_ROOT, timeout: GIT_TIMEOUT_MS, env: { ...process.env, GIT_PAGER: "cat" } };
}

async function runGit(args) {
  const { stdout } = await execFileAsync("git", args, gitArgs(args));
  return stdout.trim();
}

async function runGitCapture(args) {
  try {
    const { stdout, stderr } = await execFileAsync("git", args, gitArgs(args));
    return { ok: true, output: `${stdout}${stderr}`.trim() };
  } catch (error) {
    return { ok: false, output: `${error.stdout ?? ""}${error.stderr ?? ""}`.trim() || String(error.message) };
  }
}

const SAFE_NAME = /^[a-z0-9][a-z0-9._-]*\.yaml$/i;

function respond(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
  res.end(payload);
}

async function readBody(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  if (raw.length > 1_000_000) throw new Error("request body too large");
  return raw;
}

async function readWorkflows() {
  const { readdir } = await import("node:fs/promises");
  const entries = (await readdir(WORKFLOWS_DIR)).filter((name) => name.endsWith(".yaml")).sort();
  const workflows = [];
  for (const source of entries) {
    try {
      const parsed = yaml.load(await readFile(path.join(WORKFLOWS_DIR, source), "utf8"));
      if (!parsed || typeof parsed !== "object") continue;
      workflows.push({
        id: String(parsed.id ?? source.replace(/\.yaml$/, "")),
        enabled: parsed.enabled !== false,
        source,
        connector: parsed.trigger?.connector ?? "?",
        event: parsed.trigger?.event ?? "?",
        actionCount: Array.isArray(parsed.actions) ? parsed.actions.length : 0
      });
    } catch {
      workflows.push({ id: source, enabled: false, source, connector: "invalid", event: "yaml parse error", actionCount: 0 });
    }
  }
  return workflows;
}

async function handleApi(req, res, url) {
  const route = `${req.method} ${url.pathname}`;

  if (route === "GET /api/workflows") {
    return respond(res, 200, { workflows: await readWorkflows() });
  }

  const workflowMatch = url.pathname.match(/^\/api\/workflows\/([^/]+)$/);
  if (workflowMatch && req.method === "GET") {
    const source = decodeURIComponent(workflowMatch[1]);
    if (!SAFE_NAME.test(source)) return respond(res, 400, { error: "invalid workflow file name" });
    const full = path.join(WORKFLOWS_DIR, source);
    if (!existsSync(full)) return respond(res, 404, { error: `no such workflow: ${source}` });
    const workflow = yaml.load(await readFile(full, "utf8"));
    return respond(res, 200, { workflow });
  }

  if (route === "PUT /api/workflows") {
    const body = JSON.parse(await readBody(req));
    let workflow;
    try {
      workflow = yaml.load(body.yaml);
    } catch (error) {
      return respond(res, 400, { error: `invalid YAML: ${error.message}` });
    }
    if (!workflow || typeof workflow !== "object" || !workflow.id || !workflow.trigger) {
      return respond(res, 400, { error: "workflow needs at least id and trigger" });
    }
    const target = `${String(workflow.id).replace(/[^a-z0-9_-]+/gi, "-")}.yaml`;
    const { writeFile, unlink } = await import("node:fs/promises");
    await writeFile(path.join(WORKFLOWS_DIR, target), body.yaml, "utf8");
    const removed = [];
    if (body.renameFrom && SAFE_NAME.test(body.renameFrom) && body.renameFrom !== target) {
      await unlink(path.join(WORKFLOWS_DIR, body.renameFrom));
      await runGit(["rm", "--cached", "--quiet", `workflows/${body.renameFrom}`]).catch(() => {});
      removed.push(body.renameFrom);
    }
    await runGit(["add", `workflows/${target}`]);
    const staged = (await runGit(["diff", "--cached", "--name-only"])) !== "";
    let commit = null;
    if (staged) {
      commit = await runGit(["commit", "-m", `designer: save workflow ${workflow.id}`]);
    }
    return respond(res, 200, { saved: target, removed, commit: commit ? await runGit(["rev-parse", "HEAD"]) : null });
  }

  const deleteMatch = workflowMatch && req.method === "DELETE" ? workflowMatch[1] : null;
  if (deleteMatch) {
    const source = decodeURIComponent(deleteMatch);
    if (!SAFE_NAME.test(source)) return respond(res, 400, { error: "invalid workflow file name" });
    const { unlink } = await import("node:fs/promises");
    await unlink(path.join(WORKFLOWS_DIR, source));
    await runGit(["add", `-u`, `workflows/${source}`]).catch(() => {});
    const staged = (await runGit(["diff", "--cached", "--name-only"])) !== "";
    if (staged) await runGit(["commit", "-m", `designer: delete workflow ${source.replace(/\.yaml$/, "")}`]);
    return respond(res, 200, { deleted: source });
  }

  if (route === "GET /api/git/status") {
    const branch = await runGit(["rev-parse", "--abbrev-ref", "HEAD"]);
    const dirty = (await runGit(["status", "--porcelain"])) !== "";
    let ahead = 0;
    let behind = 0;
    try {
      const counts = (await runGit(["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])).split(/\s+/);
      behind = Number(counts[0]);
      ahead = Number(counts[1]);
    } catch {
      // no upstream configured
    }
    return respond(res, 200, { branch, dirty, ahead, behind });
  }

  if (route === "POST /api/git/push") {
    const result = await runGitCapture(["push"]);
    return respond(res, result.ok ? 200 : 409, result.ok
      ? { output: result.output || "pushed" }
      : { error: `push failed: ${result.output}` });
  }

  return respond(res, 404, { error: `no route: ${route}` });
}

function serveStatic(req, res, url) {
  const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
  const full = path.normalize(path.join(DIST_DIR, rel));
  if (!full.startsWith(DIST_DIR) || !existsSync(full)) {
    res.writeHead(404);
    return res.end("not found");
  }
  const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
  res.writeHead(200, { "content-type": types[path.extname(full)] ?? "application/octet-stream" });
  createReadStream(full).pipe(res);
}

if (UNIX_SOCKET) {
  try { unlinkSync(UNIX_SOCKET); } catch {}
}
createServer(async (req, res) => {
  const url = new URL(req.url, `http://${UNIX_SOCKET ? "localhost" : `${HOST}:${PORT}`}`);
  try {
    if (url.pathname.startsWith("/api/")) {
      await handleApi(req, res, url);
    } else {
      serveStatic(req, res, url);
    }
  } catch (error) {
    respond(res, error instanceof Error && /ENOENT|no such/.test(error.message) ? 404 : 500, { error: String(error.message ?? error) });
  }
}).listen(UNIX_SOCKET ?? PORT, UNIX_SOCKET ? undefined : HOST, () => {
  console.log(`dapier designer api on ${UNIX_SOCKET ? `unix:${UNIX_SOCKET}` : `http://${HOST}:${PORT}`} (repo: ${REPO_ROOT})`);
});
