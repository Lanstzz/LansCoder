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
    this.name = "";
    this.type = "";
    this.value = "";
    this.textContent = "";
    this.listeners = new Map();
  }

  append(...children) {
    children.forEach((child) => {
      child.parentElement = this;
      this.children.push(child);
    });
  }

  addEventListener(eventName, listener) {
    this.listeners.set(eventName, listener);
  }

  dispatch(eventName) {
    const listener = this.listeners.get(eventName);
    if (!listener) return;
    listener({ preventDefault() {} });
  }

  requestSubmit() {
    this.dispatch("submit");
  }

  get elements() {
    return Object.fromEntries(
      controls(this)
        .filter((control) => control.name)
        .map((control) => [control.name, control]),
    );
  }
}

class FakeFormData {
  constructor(form) {
    this.values = controls(form)
      .filter((control) => control.name && (control.tagName === "INPUT" || control.tagName === "SELECT"))
      .map((control) => [control.name, control.value]);
  }

  entries() {
    return this.values[Symbol.iterator]();
  }

  get(name) {
    return this.values.find(([key]) => key === name)?.[1] ?? null;
  }
}

function controls(node) {
  return node.children.flatMap((child) => [child, ...controls(child)]);
}

function control(form, name) {
  const match = controls(form).find((item) => item.name === name);
  assert.ok(match, `Expected a ${name} filter control.`);
  return match;
}

function button(form, label) {
  const match = controls(form).find((item) => item.tagName === "BUTTON" && item.textContent === label);
  assert.ok(match, `Expected a ${label} action.`);
  return match;
}

function isolatedFilterSource(script) {
  const start = script.indexOf("  const app = document.querySelector(\"#app\");");
  const end = script.indexOf("  function chips(", start);
  assert.notEqual(start, -1, "Could not locate app.js filter helpers.");
  assert.notEqual(end, -1, "Could not locate the end of app.js filter helpers.");
  return script.slice(start, end);
}

const location = {
  origin: "http://observatory.test",
  pathname: "/traces",
  search: "?status=running&tag=existing&metadata.environment=%22development%22",
};
const history = {
  calls: [],
  pushState(_state, _title, href) {
    this.calls.push(href);
    const destination = new URL(href, location.origin);
    location.pathname = destination.pathname;
    location.search = destination.search;
  },
  replaceState(_state, _title, href) {
    this.pushState(_state, _title, href);
  },
};
const app = new FakeElement("main");
const context = vm.createContext({
  URL,
  URLSearchParams,
  FormData: FakeFormData,
  document: {
    createElement: (name) => new FakeElement(name),
    querySelector: (selector) => selector === "#app" ? app : null,
  },
  history,
  location,
  renderRoute() {},
});
const source = fs.readFileSync(scriptPath, "utf8");

vm.runInContext(`${isolatedFilterSource(source)}\nglobalThis.filters = explorerFilters();`, context);

const details = context.filters;
assert.equal(details.tagName, "DETAILS");
const form = details.children.find((child) => child.tagName === "FORM");
assert.ok(form, "Expected explorerFilters() to attach its form to the disclosure.");
assert.equal(form.parentElement, details);

for (const name of [
  "project",
  "session_id",
  "from",
  "min_tokens",
  "metadata_key",
  "metadata_value",
  "status",
  "has_error",
  "tag",
]) {
  control(form, name);
}

control(form, "project").value = "project-a";
control(form, "min_tokens").value = "12";
control(form, "status").value = "failed";
control(form, "has_error").value = "true";
control(form, "metadata_key").value = "environment";
control(form, "metadata_value").value = '"production"';
control(form, "tag").value = "alpha";
button(form, "Add tag").dispatch("click");
const tags = controls(form).filter((item) => item.name === "tag");
assert.equal(tags.length, 2, "Expected Add tag to create another tag control.");
tags[1].value = "beta";

button(form, "Apply").dispatch("click");
assert.equal(
  history.calls.at(-1),
  "/traces?project=project-a&min_tokens=12&status=failed&has_error=true&tag=alpha&tag=beta&metadata.environment=%22production%22",
);

button(form, "Clear filters").dispatch("click");
assert.equal(history.calls.at(-1), "/traces");
