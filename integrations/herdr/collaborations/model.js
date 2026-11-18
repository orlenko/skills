// Display claims only. Herdr's terminal records supply every navigation target.
const LABEL = /^orlenko\.(orchestra|pair)\.[0-9a-f]{16}\.[0-9a-f]{16,64}$/;
const ID = /^[A-Za-z0-9_.:-]{1,128}$/;
const ADDRESS = /^(?:[^/\u0000-\u0020\u007f]{1,128}\/)?t[1-9][0-9bcdfghjkmnpqrstvwxyz]*$/;
const MAX_LABEL = 2048;
const MAX_LEASE = 180;

export function cleanText(value, fallback = "", limit = 80) {
  if (typeof value !== "string") return fallback;
  // Strip directional overrides and formatting as well as newlines/control bytes.
  const text = value.replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, " ").replace(/\s+/gu, " ").trim();
  return Array.from(text).slice(0, limit).join("") || fallback;
}

function machineOf(address) {
  return address.includes("/") ? address.slice(0, address.indexOf("/")) : null;
}

function available(terminal, machines) {
  if (!terminal || terminal.state !== "live" || typeof terminal.address !== "string"
      || !ADDRESS.test(terminal.address) || !Number.isSafeInteger(terminal.created_ms)
      || terminal.created_ms <= 0) return false;
  const name = machineOf(terminal.address);
  const machine = machines.find((m) => m?.name === name);
  return name === null ? !machine || machine.layout === "ready" : machine?.layout === "ready";
}

function claim(kind, value, terminal, now) {
  if (typeof value !== "string" || value.length > MAX_LABEL) return null;
  let data;
  try { data = JSON.parse(value); } catch { return null; }
  if (!data || typeof data !== "object" || Array.isArray(data) || data.v !== 1
      || typeof data.group !== "string" || !ID.test(data.group)
      || typeof data.participant !== "string" || !ID.test(data.participant)
      || !Number.isSafeInteger(data.created_ms) || data.created_ms !== terminal.created_ms
      || !Number.isSafeInteger(data.owner_pid) || data.owner_pid <= 1
      || typeof data.issued_at !== "number" || !Number.isFinite(data.issued_at)
      || typeof data.expires_at !== "number" || !Number.isFinite(data.expires_at)
      || data.issued_at <= 0 || data.issued_at > now + 30
      || data.expires_at <= now || data.expires_at > now + MAX_LEASE
      || data.expires_at <= data.issued_at || data.expires_at - data.issued_at > MAX_LEASE) return null;
  const roles = kind === "orchestra" ? ["conductor", "player"] : ["host", "guest"];
  if (!roles.includes(data.role)) return null;
  return data;
}

// Herdr owns terminal titles (what the program set, e.g. a renamed Claude session).
// They are display text only, like label names, and never a navigation target.
function sessionTitle(terminal, titles, name) {
  const raw = typeof terminal.title === "string" && terminal.title ? terminal.title : titles?.get(terminal.address);
  const title = cleanText(raw);
  return title && title !== name ? title : null;
}

export function collaborationGroups(terminals, machines, now = Date.now() / 1000, titles = new Map()) {
  const groups = new Map();
  for (const terminal of terminals) {
    if (!available(terminal, machines) || !terminal.labels || typeof terminal.labels !== "object") continue;
    for (const [publication, value] of Object.entries(terminal.labels)) {
      const match = LABEL.exec(publication);
      if (!match) continue;
      const kind = match[1];
      const data = claim(kind, value, terminal, now);
      if (!data) continue;
      const groupKey = `${kind}:${data.group}`;
      if (!groups.has(groupKey)) groups.set(groupKey, { key: groupKey, kind, id: data.group, names: [], rows: new Map() });
      const group = groups.get(groupKey);
      const groupName = cleanText(data.group_name);
      if (groupName) group.names.push(groupName);
      const key = `${groupKey}:${data.participant}:${terminal.address}`;
      const old = group.rows.get(key);
      if (old && old.issued_at > data.issued_at) continue;
      const name = cleanText(data.name, data.participant);
      const title = sessionTitle(terminal, titles, name);
      const status = ["working", "blocked", "error", "done", "idle"].includes(terminal.status) ? terminal.status : null;
      group.rows.set(key, {
        key, publication, participant: data.participant,
        address: terminal.address, created_ms: terminal.created_ms,
        issued_at: data.issued_at, name, title,
        // Pair names default to the host name, which the machine column already says;
        // an Orchestra nick is the member's identity, so it wins over the title.
        display: kind === "pair" ? title || name : name,
        role: data.role, machine: cleanText(machineOf(terminal.address), "local"), status,
      });
    }
  }
  return [...groups.values()].map((g) => ({
    key: g.key, kind: g.kind, id: g.id,
    name: g.names.sort()[0] || g.id,
    rows: [...g.rows.values()].sort((a, b) =>
      (a.role === "conductor" ? -1 : 0) - (b.role === "conductor" ? -1 : 0)
      || a.display.localeCompare(b.display) || a.address.localeCompare(b.address)
      || a.participant.localeCompare(b.participant)),
  })).sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

// A person's local name for a group wins, then a published group name, then the kind.
export function groupLabel(group, names) {
  const custom = names && typeof names === "object" ? cleanText(names[group.key], "", 60) : "";
  return custom || (group.name !== group.id ? group.name : group.kind === "orchestra" ? "Orchestra" : "Pair");
}

export function navigationFor(row, terminals, panes, machines, now = Date.now() / 1000) {
  // Recheck after a disconnect, publication retirement or terminal-id reuse.
  const current = collaborationGroups(terminals, machines, now).flatMap((g) => g.rows)
    .find((r) => r.key === row.key && r.publication === row.publication
      && r.address === row.address && r.created_ms === row.created_ms);
  if (!current) return null;
  const pane = panes.find((p) => p?.terminal === current.address && typeof p.pane === "string");
  return pane ? { command: "pane.focus", args: { pane: pane.pane } }
    : { command: "terminal.peek", args: { terminal: current.address } };
}
