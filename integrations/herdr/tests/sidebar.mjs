import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const sourceURL = new URL("../collaborations/", import.meta.url);
const dataURL = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const modelURL = dataURL(readFileSync(new URL("model.js", sourceURL), "utf8"));
const { cleanText, collaborationGroups, navigationFor } = await import(modelURL);
const NOW = 1800000000;
const machines = [{ name: null, layout: "ready" }, { name: "v", layout: "ready" }, { name: "ubuntu", layout: "ready" }];
const publication = (kind, token = "b".repeat(32)) => `orlenko.${kind}.${"a".repeat(16)}.${token}`;
const body = (overrides = {}) => ({ v: 1, group: "orch_one", participant: "mb_one", name: "Reviewer", group_name: "Skills", role: "player", expires_at: NOW + 120, created_ms: 123000, owner_pid: 1234, issued_at: NOW, ...overrides });
const terminal = (address, overrides = {}, claims = { [publication("orchestra")]: JSON.stringify(body()) }) => ({ address, created_ms: 123000, state: "live", labels: claims, status: "working", ...overrides });
const groups = (items, m = machines, now = NOW) => collaborationGroups(items, m, now);

const cross = groups([
  terminal("v/t2"),
  terminal("ubuntu/t2", {}, { [publication("orchestra")]: JSON.stringify(body({ participant: "mb_two", name: "Reviewer", role: "conductor" })) }),
]);
assert.equal(cross.length, 1);
assert.equal(cross[0].rows.length, 2);
assert.deepEqual(cross[0].rows.map((r) => r.machine), ["ubuntu", "v"]);
assert.equal(cross[0].rows[0].role, "conductor");
assert.equal(cross[0].rows[0].name, cross[0].rows[1].name, "duplicate names remain separate");

const multiple = groups([terminal("t1", {}, {
  [publication("orchestra")]: JSON.stringify(body()),
  [publication("orchestra", "c".repeat(32))]: JSON.stringify(body({ group: "orch_two" })),
  [publication("pair")]: JSON.stringify(body({ group: "pair_one", role: "host" })),
})]);
assert.equal(multiple.length, 3, "one terminal may join both kinds and several same-kind groups");
assert.equal(multiple[2].kind, "pair");
assert.equal(multiple[2].rows[0].machine, "local");

const dedup = groups([terminal("v/t2", {}, {
  [publication("orchestra")]: JSON.stringify(body({ name: "Old", issued_at: NOW - 1 })),
  [publication("orchestra", "c".repeat(32))]: JSON.stringify(body({ name: "New" })),
})]);
assert.equal(dedup[0].rows.length, 1);
assert.equal(dedup[0].rows[0].name, "New");

assert.equal(cleanText("One\nTwo\u202eSpoof\u2066\tEnd\u0000"), "One Two Spoof End");
assert.equal(cleanText("😺".repeat(100)).length, 160);
assert.equal(cleanText(3, "fallback"), "fallback");
const cleaned = groups([terminal("t1", {}, { [publication("orchestra")]: JSON.stringify(body({ name: "Reviewer\n\u202evps: boss", group_name: "My\nOrchestra" })) })]);
assert.equal(cleaned[0].name, "My Orchestra");
assert.equal(cleaned[0].rows[0].name, "Reviewer vps: boss");

const titled = (items, titles) => collaborationGroups(items, machines, NOW, titles)[0].rows[0].title;
assert.equal(titled([terminal("v/t2", { title: "skills-v" })]), "skills-v", "terminal record title");
assert.equal(titled([terminal("v/t2")], new Map([["v/t2", "skills-v"]])), "skills-v", "pane/agent title fallback");
assert.equal(titled([terminal("v/t2", { title: "own" })], new Map([["v/t2", "other"]])), "own");
assert.equal(titled([terminal("v/t2")], new Map([["ubuntu/t2", "elsewhere"]])), null, "titles match by exact address");
assert.equal(titled([terminal("v/t2", { title: "Reviewer" })]), null, "a title equal to the name is not repeated");
assert.equal(titled([terminal("v/t2", { title: "  \n\u202e " })]), null);
assert.equal(titled([terminal("v/t2", { title: 7 })]), null);
assert.equal(titled([terminal("v/t2", { title: "a\nb\u202ec" + "x".repeat(200) })]).length, 80);
assert.equal(titled([terminal("v/t2", { title: "a\nb\u202ec" })]), "a b c");

const invalidBodies = [
  "{", "null", "[]", "x".repeat(2049), JSON.stringify(body({ v: 2 })),
  JSON.stringify(body({ v: true })), JSON.stringify(body({ group: "../escape" })),
  JSON.stringify(body({ participant: "x".repeat(129) })), JSON.stringify(body({ participant: "bad\nname" })),
  JSON.stringify(body({ created_ms: 124000 })), JSON.stringify(body({ created_ms: "123000" })),
  JSON.stringify(body({ owner_pid: 1 })), JSON.stringify(body({ owner_pid: "1234" })),
  JSON.stringify(body({ issued_at: NOW + 31 })), JSON.stringify(body({ issued_at: 0 })),
  JSON.stringify(body({ issued_at: NOW - 61, expires_at: NOW + 120 })),
  JSON.stringify(body({ expires_at: NOW })), JSON.stringify(body({ expires_at: NOW + 181 })),
  JSON.stringify(body({ expires_at: "future" })), JSON.stringify(body({ expires_at: null })),
  JSON.stringify(body({ expires_at: NOW + 1, issued_at: NOW + 2 })),
  JSON.stringify(body({ role: "guest" })),
];
for (const value of invalidBodies) assert.deepEqual(groups([terminal("t1", {}, { [publication("orchestra")]: value })]), [], value);
for (const key of ["other", "orlenko.orchestra", "orlenko.orchestra../x", publication("orchestra") + ".extra"]) {
  assert.deepEqual(groups([terminal("t1", {}, { [key]: JSON.stringify(body()) })]), []);
}
assert.deepEqual(groups([terminal("t1", {}, { [publication("orchestra")]: body() })]), []);
assert.deepEqual(groups([terminal("t1")], machines, NOW + 120), []);
assert.deepEqual(groups([terminal("t1", { state: "finished" })]), []);
assert.deepEqual(groups([terminal("v/t2")], [{ name: "v", layout: "unavailable" }]), []);
assert.deepEqual(groups([terminal("v/t2")], [{ name: "v", layout: "loading" }]), []);
assert.deepEqual(groups([terminal("v/t2")], []), []);
assert.deepEqual(groups([terminal("missing/t2")]), []);
assert.deepEqual(groups([terminal("v/t2", { created_ms: null })]), []);

const live = terminal("v/t2");
const row = groups([live])[0].rows[0];
assert.deepEqual(navigationFor(row, [live], [{ terminal: "v/t2", pane: "v/pane-2" }], machines, NOW), { command: "pane.focus", args: { pane: "v/pane-2" } });
assert.deepEqual(navigationFor(row, [live], [], machines, NOW), { command: "terminal.peek", args: { terminal: "v/t2" } });
assert.equal(navigationFor(row, [], [], machines, NOW), null);
assert.equal(navigationFor(row, [terminal("v/t2", { created_ms: 999000 })], [], machines, NOW), null);
assert.equal(navigationFor(row, [live], [], [{ name: "v", layout: "unavailable" }], NOW), null);
assert.equal(navigationFor(row, [live], [], machines, NOW + 121), null);
assert.equal(navigationFor(row, [terminal("v/t2", {}, {})], [], machines, NOW), null);
const spoofed = terminal("v/t2", {}, { [publication("orchestra")]: JSON.stringify(body({ address: "ubuntu/t9", target: "rm -rf /" })) });
const spoofRow = groups([spoofed])[0].rows[0];
assert.deepEqual(navigationFor(spoofRow, [spoofed], [], machines, NOW), { command: "terminal.peek", args: { terminal: "v/t2" } });

// Load the actual main.js with small native-module doubles, rather than inventing
// a parallel UI implementation. The mock records the description and callbacks.
globalThis.__sidebar = { all: [live], panes: [{ terminal: "v/t2", pane: "v/p2", title: "pane title" }], agents: [{ address: "v/t2", title: "skills-v" }], machines, calls: [], timers: [], now: NOW };
const uiURL = dataURL(`
export class View {}
export function div() {
  const node = { nodes: [], props: {}, click: null };
  const proxy = new Proxy(node, { get(t, key) {
    if (key in t) return t[key];
    return (...args) => {
      if (key === "child") t.nodes.push(args[0]);
      else if (key === "children") t.nodes.push(...args[0]);
      else if (key === "on_click") t.click = args[0];
      else t.props[key] = args;
      return proxy;
    };
  }});
  return proxy;
}
`);
const flexURL = dataURL(`import { div } from ${JSON.stringify(uiURL)}; export const v_flex = div; export const h_flex = div;`);
const herdrURL = dataURL(`
export const terminals = () => globalThis.__sidebar.all;
export const panes = () => globalThis.__sidebar.panes;
export const agents = () => globalThis.__sidebar.agents;
export const machines = () => globalThis.__sidebar.machines;
export const run = (command,args) => globalThis.__sidebar.calls.push({command,args});
export const theme = () => ({ foreground:"#fff",dim:"#888",hover:"#222",working:"#0f0" });
`);
let main = readFileSync(new URL("main.js", sourceURL), "utf8");
main = main.replace('from "gpui-kit"', `from ${JSON.stringify(uiURL)}`)
  .replace('from "gpui-base"', `from ${JSON.stringify(flexURL)}`)
  .replace('from "herdr"', `from ${JSON.stringify(herdrURL)}`)
  .replace('from "./model.js"', `from ${JSON.stringify(modelURL)}`);
const { default: Collaborations } = await import(dataURL(main));
const oldNow = Date.now;
Date.now = () => NOW * 1000;
const state = globalThis.__sidebar;
const view = new Collaborations();
let notifications = 0;
const cx = { notify() { notifications++; }, timer: { every(ms, callback) { state.timers.push({ ms, callback }); } } };
view.init({}, cx);
assert.equal(state.timers.length, 1);
assert.equal(state.timers[0].ms, 15000);
const walk = (node) => typeof node === "object" && node !== null ? [node, ...node.nodes.flatMap(walk)] : [];
const texts = (node) => typeof node === "string" ? [node] : node && Array.isArray(node.nodes) ? node.nodes.flatMap(texts) : [];
let tree = view.render();
assert.ok(texts(tree).includes("Orchestras & Pairs"));
assert.ok(texts(tree).includes("Reviewer"));
assert.ok(texts(tree).includes("v"));
assert.ok(texts(tree).includes("Working"));
assert.ok(texts(tree).includes("skills-v"), "agents() title wins over the pane title");
assert.ok(!texts(tree).includes("pane title"));
walk(tree).find((n) => n.props.id?.[0] === `member-${row.key}`).click();
assert.deepEqual(state.calls.pop(), { command: "pane.focus", args: { pane: "v/p2" } });
state.panes = [];
view.activate(row);
assert.deepEqual(state.calls.pop(), { command: "terminal.peek", args: { terminal: "v/t2" } });
state.all = [];
view.activate(row);
assert.equal(state.calls.length, 0);
assert.ok(texts(view.render()).includes("No published collaborations"));
state.all = [live];
tree = view.render();
walk(tree).find((n) => n.props.id?.[0] === "toggle-orchestra:orch_one").click(null, cx);
assert.equal(notifications, 1);
assert.ok(!texts(view.render()).includes("Reviewer"));
state.timers[0].callback(cx);
assert.equal(notifications, 2);
Date.now = oldNow;
delete globalThis.__sidebar;
console.log(`Herdr sidebar model, navigation and rendering passed (${fileURLToPath(sourceURL)})`);
