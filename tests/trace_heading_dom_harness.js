"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const scriptPath = process.argv[2];

if (!scriptPath) {
  throw new Error("Expected the app.js path as the first argument.");
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.className = "";
    this.listeners = new Map();
    this._textContent = "";
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this._textContent = String(value);
  }

  toString() {
    return "[object HTMLElement]";
  }

  append(...children) {
    children.forEach((child) => {
      child.parentElement = this;
      this.children.push(child);
    });
  }

  replaceChildren(...children) {
    this.children = [];
    this._textContent = "";
    this.append(...children);
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, listener);
  }

  setAttribute() {}
}

function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

function isolatedDetailSource(script) {
  const start = script.indexOf("  const app = document.querySelector(\"#app\");");
  const end = script.indexOf("\n\n  async function renderSessions()", start);
  assert.notEqual(start, -1, "Could not locate app.js helpers.");
  assert.notEqual(end, -1, "Could not locate renderDetail() in app.js.");
  return script.slice(start, end);
}

const app = new FakeElement("main");
const location = {
  origin: "http://observatory.test",
  pathname: "/traces/trc_complete",
  search: "?observation=obs_generation&tab=input",
};
const context = vm.createContext({
  AbortController,
  URL,
  URLSearchParams,
  app,
  document: {
    createElement: (name) => new FakeElement(name),
    querySelector: (selector) => selector === "#app" ? app : null,
  },
  fetch: async () => ({
    ok: true,
    json: async () => ({
      trace_id: "trc_complete",
      status: "completed",
      evidence: "complete",
      started_at: null,
      duration_ms: 10,
      model: "model-a",
      provider: "fake",
      total_tokens: 1,
      branch_state: "active",
      input: null,
      final_output: null,
      metadata: null,
      relations: [],
      observations: [],
    }),
  }),
  location,
  setTimeout,
});
const source = fs.readFileSync(scriptPath, "utf8");

async function run() {
  vm.runInContext(`${isolatedDetailSource(source)}\nglobalThis.renderDetail = renderDetail;`, context);
  await context.renderDetail("trc_complete");

  const heading = descendants(app).find((node) => node.tagName === "H1" && node.textContent === "Trace detail");
  assert.ok(heading, "Expected the Trace detail heading.");
  const detail = heading.parentElement.children.find((node) => node.tagName === "P");
  assert.ok(detail, "Expected the heading to include the trace identifier.");
  assert.equal(detail.textContent, "trc_complete");
  assert.notEqual(detail.textContent, "[object HTMLElement]");
  assert.equal(detail.children.length, 1);
  assert.equal(detail.children[0].tagName, "CODE");
  assert.equal(detail.children[0].textContent, "trc_complete");
}

run();
