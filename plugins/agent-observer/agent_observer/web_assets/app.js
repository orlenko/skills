"use strict";

const viewsBreakpoint = window.matchMedia("(max-width: 74rem)");
const viewsPreferenceKey = "agent-observer:views-collapsed";

const initialViewsCollapsed = () => {
  try {
    const stored = window.localStorage.getItem(viewsPreferenceKey);
    if (stored === "true" || stored === "false") return stored === "true";
  } catch (_error) {
    // A blocked storage API should not prevent the dashboard from loading.
  }
  return viewsBreakpoint.matches;
};

const state = {
  data: null,
  view: "all",
  sort: "signal",
  search: "",
  busy: false,
  selectedProjectId: null,
  projectOrder: [],
  focusAfterRender: null,
  pendingRemovalProjectId: null,
  candidates: [],
  candidatesLoading: false,
  candidateActiveIndex: -1,
  openDetails: new Set(),
  viewsCollapsed: initialViewsCollapsed(),
  error: null,
};

const viewDefinitions = [
  ["all", "All projects"],
  ["needs_a_look", "Needs a look"],
  ["review_suggested", "Review suggested"],
  ["recently_active", "Recently active"],
  ["quiet", "Quiet"],
  ["stale", "Stale"],
  ["observer_issues", "Observer issues"],
];

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const humanLabel = (value) => {
  const words = String(value || "unknown").replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};

const plainPreview = (value) => String(value || "")
  .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
  .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
  .replace(/(^|\s)[#>-]+\s+/gm, "$1")
  .replace(/[`*_~]/g, "")
  .replace(/\s+/g, " ")
  .trim();

const appendInlineMarkdown = (parent, value) => {
  const text = String(value || "");
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\[[^\]\n]+\]\([^)\n]+\)|\*[^*\n]+\*|_[^_\n]+_)/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) parent.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      parent.append(el("code", "", token.slice(1, -1)));
    } else if (token.startsWith("**") || token.startsWith("__")) {
      parent.append(el("strong", "", token.slice(2, -2)));
    } else if (token.startsWith("~~")) {
      parent.append(el("s", "", token.slice(2, -2)));
    } else if (token.startsWith("[")) {
      parent.append(el("span", "markdown-link", token.slice(1, token.indexOf("]"))));
    } else {
      parent.append(el("em", "", token.slice(1, -1)));
    }
    offset = match.index + token.length;
  }
  if (offset < text.length) parent.append(document.createTextNode(text.slice(offset)));
};

const renderMarkdown = (value, className = "") => {
  const root = el("div", `message-markdown ${className}`.trim());
  const lines = String(value || "").replaceAll("\r\n", "\n").split("\n");
  const isBlockStart = (line) => (
    /^\s*```/.test(line)
    || /^\s{0,3}#{1,4}\s+/.test(line)
    || /^\s{0,3}>\s?/.test(line)
    || /^\s*[-*+]\s+/.test(line)
    || /^\s*\d+\.\s+/.test(line)
  );
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = line.match(/^\s*```\s*([^\s`]*)/);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = el("pre", "markdown-code-block");
      if (fence[1]) pre.append(el("span", "code-language", fence[1]));
      pre.append(el("code", "", codeLines.join("\n")));
      root.append(pre);
      continue;
    }
    const heading = line.match(/^\s{0,3}#{1,4}\s+(.+)$/);
    if (heading) {
      const block = el("p", "markdown-heading");
      appendInlineMarkdown(block, heading[1]);
      root.append(block);
      index += 1;
      continue;
    }
    if (/^\s{0,3}>\s?/.test(line)) {
      const quote = el("blockquote", "markdown-blockquote");
      const quoteLines = [];
      while (index < lines.length && /^\s{0,3}>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
        index += 1;
      }
      appendInlineMarkdown(quote, quoteLines.join(" "));
      root.append(quote);
      continue;
    }
    const unordered = /^\s*[-*+]\s+/.test(line);
    const ordered = /^\s*\d+\.\s+/.test(line);
    if (unordered || ordered) {
      const list = el(ordered ? "ol" : "ul", "markdown-list");
      const itemPattern = ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/;
      while (index < lines.length) {
        const item = lines[index].match(itemPattern);
        if (!item) break;
        const row = el("li");
        appendInlineMarkdown(row, item[1]);
        list.append(row);
        index += 1;
      }
      root.append(list);
      continue;
    }
    const paragraphLines = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    const paragraph = el("p");
    appendInlineMarkdown(paragraph, paragraphLines.join(" "));
    root.append(paragraph);
  }
  return root;
};

const relativeAge = (seconds) => {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) {
    return "activity unknown";
  }
  const age = Math.max(0, Number(seconds));
  if (age < 60) return `${Math.round(age)}s ago`;
  if (age < 3600) return `${Math.round(age / 60)}m ago`;
  if (age < 86400) return `${Math.round(age / 3600)}h ago`;
  return `${Math.round(age / 86400)}d ago`;
};

const absoluteTime = (value) => {
  if (value === null || value === undefined) return "Time unavailable";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
};

const post = async (path, body) => {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Agent-Observer": "1" },
    body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
};

const toast = (message) => {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.hidden = false;
  window.setTimeout(() => { node.hidden = true; }, 2800);
};

const renderError = () => {
  const node = document.querySelector("#error");
  node.replaceChildren();
  node.hidden = !state.error;
  if (!state.error) return;
  const copy = el("div");
  copy.append(el("strong", "", "Observer could not complete that action"));
  copy.append(el("p", "", state.error.message));
  const dismiss = el("button", "quiet-button", "Dismiss");
  dismiss.type = "button";
  dismiss.dataset.focusKey = "dismiss-error";
  dismiss.addEventListener("click", () => {
    state.error = null;
    renderError();
  });
  node.append(copy, dismiss);
};

const showError = (error, kind = "mutation") => {
  state.error = {
    kind,
    message: error instanceof Error ? error.message : String(error),
  };
  renderError();
};

const clearRefreshError = () => {
  if (state.error?.kind === "refresh") {
    state.error = null;
    renderError();
  }
};

const copyText = async (value, successMessage) => {
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      copied = true;
    } else {
      const area = el("textarea", "copy-fallback");
      area.value = value;
      area.setAttribute("readonly", "");
      document.body.append(area);
      area.select();
      copied = document.execCommand("copy");
      area.remove();
    }
  } catch (_error) {
    copied = false;
  }
  if (!copied) {
    showError(new Error("The clipboard was unavailable. Select the value in the inspector and copy it manually."));
    return;
  }
  toast(successMessage);
};

const badge = (text, kind = "") => el("span", `badge ${kind}`.trim(), text);

const persistentDetails = (className, key) => {
  const node = el("details", className);
  node.open = state.openDetails.has(key);
  node.addEventListener("toggle", () => {
    if (node.open) state.openDetails.add(key);
    else state.openDetails.delete(key);
  });
  return node;
};

const lifecycleFindings = (project) => project.findings
  .filter((finding) => finding.kind !== "decision_requested")
  .sort((left, right) => (right.updated_at || 0) - (left.updated_at || 0));

const latestProjectSession = (project) => project.sessions.reduce((latest, session) => {
  if (!latest) return session;
  const latestActivity = Number(latest.last_activity_at) || 0;
  const sessionActivity = Number(session.last_activity_at) || 0;
  return sessionActivity > latestActivity ? session : latest;
}, null);

const pullRequestLabel = (project) => {
  const raw = project.pull_request_id
    ?? project.pull_request?.number
    ?? project.pr_id
    ?? null;
  if (raw === null || raw === undefined || String(raw).trim() === "") return null;
  const value = String(raw).replace(/^#/, "");
  return `PR #${value}`;
};

const directoryName = (project) => {
  const parts = String(project.resolved_path || project.display_path || "")
    .split("/")
    .filter(Boolean);
  return parts[parts.length - 1] || project.display_path || "Unknown project";
};

const projectIdentity = (project) => {
  const session = latestProjectSession(project);
  const identifiers = [
    session?.title?.trim() || null,
    project.current_branch || null,
    pullRequestLabel(project),
    directoryName(project),
  ].filter(Boolean);
  return {
    primary: identifiers[0],
    secondary: identifiers.slice(1),
    session,
  };
};

const candidateTitle = (candidate) => (
  candidate.session?.title?.trim() || candidate.directory_name || "Unnamed project"
);

const candidateMatches = () => {
  const query = document.querySelector("#project-path").value.trim().toLocaleLowerCase();
  if (!query) return state.candidates.slice(0, 10);
  return state.candidates.filter((candidate) => [
    candidateTitle(candidate),
    candidate.resolved_path,
    candidate.directory_name,
    candidate.branch || "",
    candidate.session?.topic || "",
    candidate.session?.provider || "",
  ].join(" ").toLocaleLowerCase().includes(query)).slice(0, 10);
};

const closeProjectOptions = () => {
  const options = document.querySelector("#project-options");
  options.hidden = true;
  const input = document.querySelector("#project-path");
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  state.candidateActiveIndex = -1;
};

const chooseCandidate = (candidate) => {
  const input = document.querySelector("#project-path");
  input.value = candidate.resolved_path;
  closeProjectOptions();
  input.focus();
};

const renderProjectOptions = () => {
  const options = document.querySelector("#project-options");
  const input = document.querySelector("#project-path");
  if (document.querySelector("#enrollment").hidden) {
    closeProjectOptions();
    return;
  }
  options.replaceChildren();
  options.hidden = false;
  input.setAttribute("aria-expanded", "true");
  if (state.candidatesLoading) {
    options.append(el("p", "project-options-empty", "Finding recent projects…"));
    return;
  }
  const matches = candidateMatches();
  if (!matches.length) {
    options.append(el(
      "p",
      "project-options-empty",
      state.candidates.length
        ? "No unwatched recent project matches. You can still paste an exact directory."
        : "No recent unwatched projects found. You can still paste an exact directory.",
    ));
    return;
  }
  if (state.candidateActiveIndex >= matches.length) state.candidateActiveIndex = matches.length - 1;
  for (const [index, candidate] of matches.entries()) {
    const option = el("button", "project-option");
    option.type = "button";
    option.id = `project-option-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", String(index === state.candidateActiveIndex));
    const head = el("span", "project-option-head");
    head.append(el("strong", "project-option-title", candidateTitle(candidate)));
    if (candidate.session?.provider) {
      head.append(el("span", "project-option-provider", candidate.session.provider));
    }
    head.append(el(
      "span",
      "project-option-age",
      relativeAge(Date.now() / 1000 - candidate.last_activity_at),
    ));
    option.append(head);
    const metadata = [candidate.branch, candidate.directory_name].filter(Boolean).join(" · ");
    if (metadata) option.append(el("span", "project-option-meta", metadata));
    option.append(el("span", "project-option-path", candidate.resolved_path));
    if (candidate.session?.topic) {
      option.append(el("span", "project-option-topic", candidate.session.topic));
    }
    option.addEventListener("click", () => chooseCandidate(candidate));
    options.append(option);
  }
  if (state.candidateActiveIndex >= 0) {
    input.setAttribute("aria-activedescendant", `project-option-${state.candidateActiveIndex}`);
    options.children[state.candidateActiveIndex]?.scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
};

const loadProjectCandidates = async () => {
  state.candidatesLoading = true;
  renderProjectOptions();
  try {
    const response = await fetch("/api/project-candidates", { credentials: "same-origin" });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
    state.candidates = Array.isArray(value.candidates) ? value.candidates : [];
  } catch (error) {
    state.candidates = [];
    showError(error);
  } finally {
    state.candidatesLoading = false;
    state.candidateActiveIndex = -1;
    renderProjectOptions();
  }
};

const dismissProjectAttention = async (project, control) => {
  try {
    control.disabled = true;
    state.data = await post("/api/projects/dismiss-attention", { project: project.project_id });
    state.focusAfterRender = `dismiss:${project.project_id}`;
    toast("Project attention dismissed; newer findings will surface it again");
    render();
  } catch (error) {
    showError(error);
  } finally {
    control.disabled = false;
  }
};

const removeProject = async (project, control) => {
  const priorIndex = state.data.projects.findIndex((item) => item.project_id === project.project_id);
  try {
    control.disabled = true;
    state.data = await post("/api/projects/remove", { project: project.project_id });
    state.pendingRemovalProjectId = null;
    if (state.selectedProjectId === project.project_id) {
      const next = state.data.projects[Math.min(priorIndex, state.data.projects.length - 1)] || null;
      state.selectedProjectId = next?.project_id || null;
      state.focusAfterRender = next ? `project:${next.project_id}` : "watch-toggle";
    }
    toast("Stopped watching project; worker files were not changed");
    render();
  } catch (error) {
    showError(error);
  } finally {
    control.disabled = false;
  }
};

const projectRemovalButton = (project, location, label) => {
  const armed = state.pendingRemovalProjectId === project.project_id;
  const control = el(
    "button",
    `${location === "row" ? "queue-remove" : "quiet-button stop-watching"}${armed ? " armed" : ""}`,
    armed ? "Confirm" : label,
  );
  control.type = "button";
  control.dataset.focusKey = `remove:${location}:${project.project_id}`;
  control.setAttribute("aria-label", armed
    ? `Confirm stop watching ${directoryName(project)}`
    : `Stop watching ${directoryName(project)}`);
  control.title = armed
    ? "Confirm removal of Observer-owned state"
    : "Stops observation and removes cached Observer state; worker files are untouched";
  control.addEventListener("click", () => {
    if (armed) {
      removeProject(project, control);
      return;
    }
    state.pendingRemovalProjectId = project.project_id;
    state.focusAfterRender = `remove:${location}:${project.project_id}`;
    render();
    window.setTimeout(() => {
      if (state.pendingRemovalProjectId !== project.project_id) return;
      state.pendingRemovalProjectId = null;
      render();
    }, 6000);
  });
  return control;
};

const isEmptyUnavailableProject = (project) => (
  project.facets.health === "unavailable"
  && project.sessions.length === 0
  && project.sources.length === 0
  && project.findings.length === 0
);

const projectSignal = (project) => {
  const attention = project.primary_attention;
  if (attention) {
    const observed = attention.truth === "observed";
    return {
      kind: observed ? "observed" : "model",
      label: attention.type === "decision"
        ? "Decision to make"
        : attention.type === "question"
          ? "Question to answer"
          : attention.type === "requested_user_action"
            ? "Action requested"
            : "Input requested",
      summary: plainPreview(attention.title),
      source: observed
        ? `${attention.provider} · ${attention.session_id.slice(0, 8)}`
        : `Model review · ${humanLabel(attention.review_status)}`,
      attention,
    };
  }
  if (project.facets.suggested_review_items > 0 && project.review) {
    return {
      kind: "model",
      label: "Review suggested",
      summary: plainPreview(project.review.summary || "Conversation review found a lower-priority loose end."),
      source: `Model review · ${humanLabel(project.review.status)}`,
    };
  }
  if (project.facets.health !== "healthy") {
    return {
      kind: "unknown",
      label: "Observer",
      summary: "Collection needs inspection.",
      source: humanLabel(project.facets.health),
    };
  }
  if (project.facets.activity === "recent") {
    return {
      kind: "derived",
      label: "Derived",
      summary: "Recent agent activity. No unseen factual finding.",
      source: `${project.sessions.length} known session${project.sessions.length === 1 ? "" : "s"}`,
    };
  }
  return {
    kind: "derived",
    label: "Derived",
    summary: "No current attention signal.",
    source: `${project.sessions.length} known session${project.sessions.length === 1 ? "" : "s"}`,
  };
};

const sortKey = (project) => {
  const attention = project.primary_attention;
  if (attention) return [attention.truth === "observed" ? 0 : 5, project.facets.activity_age_seconds ?? Infinity];
  if (project.facets.suggested_review_items > 0) return [10, project.facets.activity_age_seconds ?? Infinity];
  if (project.facets.health !== "healthy") return [20, project.facets.activity_age_seconds ?? Infinity];
  if (project.facets.activity === "recent") return [30, project.facets.activity_age_seconds ?? Infinity];
  if (project.facets.activity === "quiet") return [40, project.facets.activity_age_seconds ?? Infinity];
  if (project.facets.activity === "stale") return [50, project.facets.activity_age_seconds ?? Infinity];
  return [60, project.facets.activity_age_seconds ?? Infinity];
};

const filteredProjects = () => {
  if (!state.data) return [];
  const query = state.search.trim().toLocaleLowerCase();
  return state.data.projects.filter((project) => {
    if (state.view !== "all" && !project.views.includes(state.view)) return false;
    if (!query) return true;
    const haystack = [
      project.display_path,
      project.resolved_path,
      project.current_branch || "",
      project.node?.display_name || "",
      ...project.findings.map((finding) => finding.summary),
      project.review?.summary || "",
      ...project.sessions.flatMap((session) => [session.provider, session.session_id, session.title || ""]),
    ].join(" ").toLocaleLowerCase();
    return haystack.includes(query);
  });
};

const orderedProjects = () => {
  const projects = filteredProjects();
  const sorted = [...projects].sort((left, right) => {
    if (state.sort === "project") {
      const leftIdentity = projectIdentity(left).primary || "";
      const rightIdentity = projectIdentity(right).primary || "";
      return leftIdentity.localeCompare(rightIdentity)
        || left.resolved_path.localeCompare(right.resolved_path);
    }
    if (state.sort === "activity") {
      return (left.facets.activity_age_seconds ?? Infinity)
        - (right.facets.activity_age_seconds ?? Infinity)
        || left.resolved_path.localeCompare(right.resolved_path);
    }
    const [leftRank, leftAge] = sortKey(left);
    const [rightRank, rightAge] = sortKey(right);
    return leftRank - rightRank || leftAge - rightAge || left.display_path.localeCompare(right.display_path);
  });
  if (state.sort === "project") {
    const present = new Set(state.projectOrder);
    state.projectOrder.push(
      ...sorted
        .filter((project) => !present.has(project.project_id))
        .map((project) => project.project_id),
    );
    const position = new Map(state.projectOrder.map((id, index) => [id, index]));
    return [...projects].sort(
      (left, right) => position.get(left.project_id) - position.get(right.project_id),
    );
  }
  state.projectOrder = [];
  return sorted;
};

const renderSortControls = () => {
  for (const control of document.querySelectorAll("[data-sort]")) {
    control.setAttribute("aria-pressed", String(control.dataset.sort === state.sort));
  }
};

const renderViews = () => {
  const nav = document.querySelector("#views");
  nav.replaceChildren();
  for (const [key, label] of viewDefinitions) {
    const button = el("button", "view-button");
    button.type = "button";
    button.dataset.focusKey = `view:${key}`;
    button.setAttribute("aria-current", String(state.view === key));
    button.append(el("span", "", label));
    const count = key === "all"
      ? (state.data?.projects.length || 0)
      : (state.data?.view_counts[key] || 0);
    button.append(el("span", "view-count", String(count)));
    button.addEventListener("click", () => {
      state.view = key;
      render();
    });
    nav.append(button);
  }
};

const syncViewsDisclosure = () => {
  const collapsed = viewsBreakpoint.matches && state.viewsCollapsed;
  const shell = document.querySelector(".shell");
  const nav = document.querySelector("#views");
  const toggle = document.querySelector("#views-toggle");
  shell.classList.toggle("views-collapsed", collapsed);
  nav.hidden = collapsed;
  toggle.hidden = !viewsBreakpoint.matches;
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.textContent = collapsed ? "Show views" : "Hide views";
};

const setViewsCollapsed = (collapsed) => {
  state.viewsCollapsed = collapsed;
  try {
    window.localStorage.setItem(viewsPreferenceKey, String(collapsed));
  } catch (_error) {
    // Keep the in-memory preference when storage is unavailable.
  }
  syncViewsDisclosure();
};

const renderProjectRow = (project) => {
  const signal = projectSignal(project);
  const identityValue = projectIdentity(project);
  const row = el("div", "project-row");
  const selected = project.project_id === state.selectedProjectId;
  if (selected) row.classList.add("selected");

  const button = el("button", "project-select");
  button.type = "button";
  button.dataset.projectId = project.project_id;
  button.dataset.focusKey = `project:${project.project_id}`;
  button.setAttribute("aria-pressed", String(selected));
  button.setAttribute("aria-controls", "inspector");

  const identity = el("span", "project-identity");
  const identityTitle = el("span", "identity-title");
  identityTitle.append(el("strong", "session-name", identityValue.primary));
  if (identityValue.session) {
    identityTitle.append(el("span", "session-provider", humanLabel(identityValue.session.provider)));
  }
  if (project.node?.display_name) {
    identityTitle.append(badge(project.node.display_name, "host-badge"));
  }
  identity.append(identityTitle);
  const identityMeta = el("span", "identity-meta");
  for (const [index, value] of identityValue.secondary.entries()) {
    if (index) identityMeta.append(document.createTextNode(" · "));
    identityMeta.append(el("span", index === 0 && project.current_branch === value ? "branch" : "", value));
  }
  identity.append(identityMeta);

  const claim = el("span", "project-claim");
  const claimMeta = el("span", "claim-meta");
  claimMeta.append(badge(signal.label, `truth-${signal.kind}`));
  if (project.facets.health !== "healthy") {
    claimMeta.append(badge(`Observer ${project.facets.health}`, "health-issue"));
  }
  if (signal.attention?.truth === "model") {
    claimMeta.append(badge("Model review", "truth-model"));
  }
  claim.append(claimMeta, el("strong", "claim-summary", signal.summary), el("span", "claim-source", signal.source));

  const age = el("span", "project-age");
  age.append(el("strong", "", relativeAge(project.facets.activity_age_seconds)));
  age.append(el("span", "", humanLabel(project.facets.activity)));

  button.append(identity, claim, age);
  button.addEventListener("click", () => {
    state.selectedProjectId = project.project_id;
    render();
  });
  row.append(button);
  if (signal.attention) {
    const dismiss = el("button", "queue-dismiss");
    dismiss.type = "button";
    dismiss.dataset.focusKey = `dismiss:${project.project_id}`;
    dismiss.setAttribute("aria-label", `Dismiss current attention for ${identityValue.primary}`);
    dismiss.title = "Dismiss all current attention for this project; newer findings will surface it again";
    dismiss.append(el("span", "checkbox-mark", ""), el("span", "", "Dismiss"));
    dismiss.addEventListener("click", () => dismissProjectAttention(project, dismiss));
    row.append(dismiss);
  } else if (project.origin !== "remote" && isEmptyUnavailableProject(project)) {
    row.append(projectRemovalButton(project, "row", "Remove"));
  } else {
    row.append(el("span", "queue-action-empty", ""));
  }
  return row;
};

const sectionHeading = (title, truth, truthClass = "") => {
  const heading = el("div", "section-heading");
  heading.append(el("h3", "", title));
  if (truth) heading.append(badge(truth, truthClass));
  return heading;
};

const attentionTypeLabel = (attention) => {
  if (attention.type === "decision") return "Decision";
  if (attention.type === "question") return "Question";
  if (attention.type === "requested_user_action") return "Requested action";
  return "Input requested";
};

const renderAttentionItem = (attention, project) => {
  const row = el("article", `attention-item truth-${attention.truth}`);
  const heading = el("div", "item-heading");
  heading.append(el("h4", "", attention.title || attentionTypeLabel(attention)));
  heading.append(badge(
    attention.truth === "observed" ? "Observed request" : "Model review",
    attention.truth === "observed" ? "truth-observed" : "truth-model",
  ));
  row.append(heading);
  if (attention.detail && attention.detail !== attention.title) {
    row.append(renderMarkdown(attention.detail, "item-summary"));
  }

  const structured = attention.details?.items;
  const items = Array.isArray(structured)
    ? structured
    : (structured && typeof structured === "object" ? Object.values(structured) : []);
  if (items.length) {
    const list = el("div", "structured-questions");
    for (const item of items.filter((value) => value.state !== "resolved")) {
      const question = el("div", "structured-question");
      const questionHead = el("p", "question-heading");
      questionHead.append(el("strong", "", item.question || "Question"));
      questionHead.append(badge(humanLabel(item.state || "open"), "local-state"));
      question.append(questionHead);
      if (Array.isArray(item.options) && item.options.length) {
        const options = el("ul", "option-list");
        for (const option of item.options) {
          const optionRow = el("li");
          optionRow.append(el("strong", "", option.label || "Option"));
          if (option.description) optionRow.append(document.createTextNode(`: ${option.description}`));
          options.append(optionRow);
        }
        question.append(options);
      }
      list.append(question);
    }
    row.append(list);
  }

  const evidence = persistentDetails(
    "attention-evidence",
    `attention-evidence:${project.project_id}:${attention.attention_id}`,
  );
  evidence.append(el("summary", "", "Evidence and provenance"));
  if (attention.evidence_excerpt) {
    const quote = el("blockquote", "evidence-quote");
    quote.append(renderMarkdown(attention.evidence_excerpt, "evidence-markdown"));
    evidence.append(quote);
  }
  const source = [
    attention.provider || "unknown provider",
    attention.session_id ? attention.session_id.slice(0, 8) : "unknown session",
    absoluteTime(attention.updated_at),
  ];
  if (attention.truth === "model") {
    source.push(`${humanLabel(attention.review_status)} bounded review`);
  }
  evidence.append(el("p", "provenance", source.join(" · ")));
  row.append(evidence);
  return row;
};

const renderAttention = (project) => {
  const section = el("section", "inspector-section attention-section");
  const items = project.attention_items || [];
  section.append(sectionHeading(
    "Needs your input",
    items.length ? `${items.length} open` : "Clear",
    items.length ? "attention-count" : "health-good",
  ));
  if (!items.length) {
    section.append(el("p", "empty-copy", "No unresolved human input is currently identified."));
    return section;
  }
  for (const item of items) section.append(renderAttentionItem(item, project));
  const dismiss = el("button", "quiet-button", "Dismiss current attention");
  dismiss.type = "button";
  dismiss.dataset.focusKey = `seen:${project.project_id}`;
  dismiss.title = "Dismiss this attention set; a newer observed request or review will surface it again";
  dismiss.addEventListener("click", () => dismissProjectAttention(project, dismiss));
  section.append(dismiss);
  return section;
};

const renderReview = (project) => {
  const review = project.review;
  const section = el("section", "inspector-section model-review");
  section.append(sectionHeading("Analysis context", "Model review", "truth-model"));
  if (!review) {
    section.append(el("p", "empty-copy", "No subscription-backed review has been published for this project."));
    return section;
  }
  section.append(renderMarkdown(review.summary || "Review prepared without a summary.", "review-summary"));
  const model = review.analyzer_model ? ` · ${review.analyzer_model}` : "";
  section.append(el(
    "p",
    "provenance",
    `${review.analyzer_provider}${model} · subscription analysis · ${humanLabel(review.status)}`,
  ));
  if (review.target_session) {
    const target = `${review.target_session.provider}:${review.target_session.session_id.slice(0, 8)}`;
    const count = review.coverage?.message_count ?? 0;
    const limit = review.coverage?.message_limit ?? 40;
    section.append(el("p", "coverage", `Target ${target}. Reviewed ${count} of at most ${limit} visible messages.`));
  }
  for (const gap of review.coverage?.gaps || []) {
    section.append(el("p", "coverage issue-text", `Coverage gap: ${gap}`));
  }
  const displayed = new Set(
    (project.attention_items || [])
      .filter((item) => item.truth === "model")
      .map((item) => item.review_item_index),
  );
  const otherItems = (review.items || []).filter((_item, index) => !displayed.has(index));
  const notes = persistentDetails("review-notes", `review-notes:${project.project_id}`);
  notes.append(el("summary", "", `Other review notes (${otherItems.length})`));
  for (const item of otherItems) {
    const row = el("article", "review-item");
    const head = el("div", "item-heading");
    head.append(el("h4", "", item.title));
    head.append(badge(humanLabel(item.assessment), "truth-model"));
    row.append(head, renderMarkdown(item.detail, "item-summary"));
    if (item.evidence_excerpt) {
      const quote = el("blockquote", "evidence-quote");
      quote.append(renderMarkdown(item.evidence_excerpt, "evidence-markdown"));
      row.append(quote);
    }
    row.append(el(
      "p",
      "provenance",
      `${item.provider || review.target_session?.provider || "unknown"} · ${item.session_id.slice(0, 8)} · ${absoluteTime(item.timestamp)}`,
    ));
    notes.append(row);
  }
  if (otherItems.length) section.append(notes);
  for (const limitation of review.limitations || []) {
    section.append(el("p", "coverage", `Limit: ${limitation}`));
  }
  return section;
};

const renderActivityDiagnostics = (project) => {
  const findings = lifecycleFindings(project);
  if (!findings.length) return null;
  const section = persistentDetails(
    "inspector-section activity-diagnostics",
    `activity-diagnostics:${project.project_id}`,
  );
  const heading = el("summary", "diagnostics-summary");
  heading.append(
    el("strong", "", "Activity history and diagnostics"),
    el("span", "", `${findings.length} lifecycle event${findings.length === 1 ? "" : "s"}`),
  );
  section.append(heading);
  section.append(el(
    "p",
    "empty-copy diagnostics-intro",
    "Turn lifecycle and raw worker output are retained for troubleshooting; they do not imply that you owe a response.",
  ));
  for (const finding of findings.slice(0, 50)) {
    const row = el("article", "diagnostic-row");
    const head = el("div", "item-heading");
    head.append(el("h4", "", humanLabel(finding.kind)));
    head.append(el("span", "provenance", absoluteTime(finding.updated_at)));
    row.append(head);
    if (finding.summary && !["Turn completed", "Turn aborted"].includes(finding.summary)) {
      const raw = persistentDetails(
        "raw-payload",
        `raw-payload:${project.project_id}:${finding.finding_id}`,
      );
      raw.append(el("summary", "", "Recorded output"));
      const pre = el("pre", "markdown-code-block");
      pre.append(el("code", "", finding.summary));
      raw.append(pre);
      row.append(raw);
    }
    section.append(row);
  }
  if (findings.length > 50) {
    section.append(el("p", "coverage", `${findings.length - 50} older events are not shown.`));
  }
  return section;
};

const renderSources = (project) => {
  const section = el("section", "inspector-section");
  section.append(sectionHeading("Source health", "Derived", "truth-derived"));
  if (!project.sources.length) {
    section.append(el("p", "empty-copy", "No bound sources. Observer coverage is unavailable."));
    return section;
  }
  for (const source of project.sources) {
    const row = el("article", "source-row");
    const head = el("div", "item-heading");
    head.append(el("h4", "", `${humanLabel(source.provider)} ${source.session_id.slice(0, 8)}`));
    head.append(badge(humanLabel(source.health), source.health === "healthy" ? "health-good" : "health-issue"));
    row.append(head);
    if (source.health_detail) row.append(el("p", "issue-text", source.health_detail));
    const diagnostics = [`generation ${source.generation ?? "unknown"}`];
    if (source.malformed_count) diagnostics.push(`${source.malformed_count} malformed records`);
    if (source.unknown_count) diagnostics.push(`${source.unknown_count} unknown records`);
    row.append(el("p", "provenance", diagnostics.join(" · ")));
    section.append(row);
  }
  return section;
};

const renderSessions = (project) => {
  const section = el("section", "inspector-section");
  section.append(sectionHeading("Known sessions", "Observed", "truth-observed"));
  if (!project.sessions.length) {
    section.append(el("p", "empty-copy", "No Claude or Codex session is currently bound to this project."));
    return section;
  }
  for (const session of project.sessions) {
    const row = el("article", "session-row");
    const sessionCopy = el("button", "copy-button", "Copy ID");
    sessionCopy.type = "button";
    sessionCopy.dataset.focusKey = `copy-session:${session.provider}:${session.session_id}`;
    sessionCopy.addEventListener("click", () => copyText(session.session_id, "Session ID copied"));
    const copy = el("div", "session-copy");
    copy.append(el("h4", "", session.title || `${humanLabel(session.provider)} session`));
    copy.append(el("code", "selectable", `${session.provider}:${session.session_id}`));
    const stateLine = [relativeAge(session.activity_age_seconds), humanLabel(session.last_turn_state || session.last_kind)];
    copy.append(el("p", "provenance", stateLine.join(" · ")));
    row.append(copy, sessionCopy);
    section.append(row);
  }
  return section;
};

const renderChanges = (project) => {
  const section = el("section", "inspector-section");
  section.append(sectionHeading("Observed changes", "Observed", "truth-observed"));
  if (!project.changes.length) {
    section.append(el("p", "empty-copy", "No supported branch, title, or focus change has been recorded."));
    return section;
  }
  for (const change of project.changes) {
    const row = el("article", "change-row");
    row.append(el("h4", "", humanLabel(change.kind)));
    row.append(el("p", "", `${change.old_value || "unknown"} → ${change.new_value || "unknown"}`));
    row.append(el("p", "provenance", absoluteTime(change.observed_at)));
    section.append(row);
  }
  return section;
};

const renderFacets = (project) => {
  const section = el("section", "inspector-section facet-section");
  section.append(sectionHeading("Current facets", "Derived", "truth-derived"));
  const list = el("dl", "facet-list");
  const values = [
    ["Attention", humanLabel(project.facets.attention)],
    ["Activity", `${humanLabel(project.facets.activity)}, ${relativeAge(project.facets.activity_age_seconds)}`],
    ["Observer health", humanLabel(project.facets.health)],
    ["Continuity", humanLabel(project.facets.continuity)],
  ];
  if (project.node) {
    values.push(
      ["Host", project.node.display_name || project.node.node_id],
      ["Remote transport", humanLabel(project.node.transport_state)],
      ["Remote analyzer", humanLabel(project.node.analyzer?.state || "detached")],
    );
    if (Number.isFinite(Number(project.node.clock_skew_seconds))) {
      values.push(["Detected clock skew", `${Math.round(Number(project.node.clock_skew_seconds))}s`]);
    }
  }
  for (const [term, value] of values) {
    list.append(el("dt", "", term), el("dd", "", value));
  }
  section.append(list);
  return section;
};

const renderInspector = (project) => {
  const inspector = document.querySelector("#inspector");
  inspector.replaceChildren();
  if (!project) {
    const empty = el("div", "inspector-empty");
    empty.append(el("p", "eyebrow", "PROJECT DETAIL"));
    empty.append(el("h2", "", "Select a watched project"));
    empty.append(el("p", "", "Evidence, sessions, health, and local actions will appear here."));
    inspector.append(empty);
    return;
  }

  const identity = projectIdentity(project);
  const header = el("header", "inspector-header");
  const detailLabel = identity.session
    ? `${humanLabel(identity.session.provider).toLocaleUpperCase()} SESSION DETAIL`
    : "PROJECT DETAIL";
  header.append(el("p", "eyebrow", detailLabel));
  header.append(el("h2", "selectable", identity.primary));
  if (project.node?.display_name) {
    const host = el("p", "remote-origin");
    host.append(
      badge(project.node.display_name, "host-badge"),
      document.createTextNode(` ${humanLabel(project.node.transport_state)} · snapshot ${project.node.last_revision || 0}`),
    );
    header.append(host);
  }
  if (identity.secondary.length) {
    header.append(el("p", "inspector-identifiers", identity.secondary.join(" · ")));
  }
  const resolved = el("code", "resolved-path selectable", project.resolved_path);
  header.append(resolved);
  const actions = el("div", "inspector-actions");
  const rescan = el("button", "quiet-button", "Rescan sources");
  rescan.type = "button";
  rescan.dataset.focusKey = `rescan:${project.project_id}`;
  rescan.addEventListener("click", async () => {
    try {
      rescan.disabled = true;
      state.data = await post("/api/rescan", { project: project.project_id });
      toast("Source discovery complete");
      render();
    } catch (error) {
      showError(error);
    } finally {
      rescan.disabled = false;
    }
  });
  if (project.origin === "remote") {
    const remoteNote = el(
      "span",
      "remote-action-note",
      `Full context and watchlist controls remain on ${project.node?.display_name || "the remote host"}`,
    );
    actions.append(remoteNote);
  } else {
    actions.append(rescan, projectRemovalButton(project, "inspector", "Stop watching"));
  }
  header.append(actions);
  inspector.append(
    header,
    renderAttention(project),
    renderFacets(project),
    renderReview(project),
    renderSessions(project),
    renderSources(project),
    renderChanges(project),
  );
  const diagnostics = renderActivityDiagnostics(project);
  if (diagnostics) inspector.append(diagnostics);
};

const renderServices = () => {
  const notices = document.querySelector("#service-notices");
  notices.replaceChildren();
  const service = document.querySelector("#service-state");
  const server = state.data?.services?.server;
  const daemon = state.data?.services?.daemon;
  const ingest = state.data?.services?.ingest;
  const analyzerService = state.data?.services?.analyzer;
  const ingestEnabled = Boolean(state.data?.services?.remote_ingest_enabled);
  const analyzer = state.data?.analyzer;
  const heartbeatAge = daemon?.heartbeat_at && state.data?.generated_at
    ? Math.max(0, state.data.generated_at - daemon.heartbeat_at)
    : null;
  const heartbeatStale = daemon?.running && (heartbeatAge === null || heartbeatAge > 15);
  const healthy = Boolean(
    server?.running
    && daemon?.running
    && !heartbeatStale
    && !daemon?.error
    && (!ingestEnabled || ingest?.running)
  );
  const analyzerActive = Boolean(analyzerService?.running)
    && ["attached", "waiting", "analyzing"].includes(analyzer?.state);
  service.className = `service-state ${healthy ? (analyzerActive ? "healthy" : "detached") : "issue"}`;
  service.lastElementChild.textContent = healthy
    ? (analyzerActive
      ? `Collector live · ${analyzer.provider || "AI"} ${analyzer.state}`
      : "Collector live · analyzer detached")
    : (heartbeatStale ? "Collector heartbeat stale" : "Observer needs attention");

  const addNotice = (title, copy, kind) => {
    const notice = el("section", `service-notice ${kind}`);
    notice.append(el("strong", "", title), el("p", "", copy));
    notices.append(notice);
  };
  if (server && !server.running) {
    addNotice("Dashboard process state is inconsistent", "The current page is available, but the stored server state says it is not running.", "error");
  }
  if (daemon && !daemon.running) {
    addNotice("Collector is not running", "Cached project information remains visible, but new session-log activity is not being collected.", "error");
  } else if (heartbeatStale) {
    addNotice("Collector heartbeat is stale", `The last recorded heartbeat was ${relativeAge(heartbeatAge)}. Displayed project data may be behind.`, "warning");
  }
  if (daemon?.error) {
    addNotice("Collector reported an error", daemon.error, "error");
  }
  if (ingestEnabled && !ingest?.running) {
    addNotice(
      "Remote ingest is not running",
      "The dashboard remains local, but enrolled LAN/Tailscale nodes cannot upload snapshots.",
      "error",
    );
  }
  if (analyzerService && !analyzerService.running) {
    addNotice(
      "Semantic analyzer is not running",
      "Factual collection continues without model calls. Start the Observer skill in Claude or Codex to restore activity-gated review.",
      "warning",
    );
  } else if (analyzerService?.error) {
    addNotice("Semantic analyzer reported an error", analyzerService.error, "warning");
  }
  for (const node of state.data?.remote_nodes || []) {
    if (["connected", "revoked"].includes(node.transport_state)) continue;
    addNotice(
      `${node.display_name || "Remote Observer"} is ${node.transport_state}`,
      "Cached findings remain visible, but new remote snapshots are not arriving. Worker state is unknown.",
      "warning",
    );
  }
  if (healthy && analyzer && !analyzerActive) {
    const reason = analyzer.stop_reason ? ` ${analyzer.stop_reason}.` : "";
    addNotice(
      "Semantic analyzer is detached",
      `Factual collection is current, but conversation loose ends are not being reviewed.${reason} Start the Observer skill in Claude or Codex to resume activity-gated review.`,
      "warning",
    );
  }
};

const findFocusTarget = (key) => {
  if (!key) return null;
  return [...document.querySelectorAll("[data-focus-key]")]
    .find((node) => node.dataset.focusKey === key) || null;
};

const render = () => {
  const active = document.activeElement;
  const focusKey = active?.dataset?.focusKey;
  const list = document.querySelector("#projects");
  const inspector = document.querySelector("#inspector");
  const listScroll = list.scrollTop;
  const inspectorScroll = inspector.scrollTop;

  renderViews();
  syncViewsDisclosure();
  renderSortControls();
  const projects = orderedProjects();
  if (!projects.some((project) => project.project_id === state.selectedProjectId)) {
    state.selectedProjectId = projects[0]?.project_id || null;
  }

  list.replaceChildren();
  list.setAttribute("aria-busy", "false");
  if (!projects.length) {
    const empty = el("div", "empty");
    empty.append(el("strong", "", state.data?.projects.length ? "Nothing in this view" : "No projects watched yet"));
    empty.append(document.createTextNode(state.data?.projects.length
      ? "Try another view or clear the filter."
      : "Watch a local project to begin building a private activity ledger."));
    list.append(empty);
  } else {
    projects.forEach((project) => list.append(renderProjectRow(project)));
  }

  const selected = projects.find((project) => project.project_id === state.selectedProjectId);
  renderInspector(selected);
  const view = viewDefinitions.find(([key]) => key === state.view);
  document.querySelector("#view-kicker").textContent = (view?.[1] || "All projects").toUpperCase();
  if (state.data) {
    const updated = document.querySelector("#updated-at");
    updated.textContent = `updated ${relativeAge(Date.now() / 1000 - state.data.generated_at)}`;
    updated.title = absoluteTime(state.data.generated_at);
  }
  renderServices();
  renderError();

  list.scrollTop = listScroll;
  inspector.scrollTop = inspectorScroll;
  const requestedFocusKey = state.focusAfterRender || focusKey;
  let focusTarget = findFocusTarget(requestedFocusKey);
  if (!focusTarget && state.focusAfterRender) {
    focusTarget = document.querySelector(".queue-dismiss")
      || document.querySelector(".project-select");
  }
  state.focusAfterRender = null;
  if (focusTarget) focusTarget.focus({ preventScroll: true });
};

const refresh = async () => {
  if (state.busy) return;
  state.busy = true;
  try {
    const response = await fetch("/api/status", { credentials: "same-origin" });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Refresh failed (${response.status})`);
    state.data = value;
    clearRefreshError();
    render();
  } catch (error) {
    showError(error, "refresh");
  } finally {
    state.busy = false;
  }
};

document.querySelector("#search").addEventListener("input", (event) => {
  state.search = event.target.value;
  render();
});

document.querySelector("#views-toggle").addEventListener("click", () => {
  setViewsCollapsed(!state.viewsCollapsed);
});

viewsBreakpoint.addEventListener("change", syncViewsDisclosure);

for (const control of document.querySelectorAll("[data-sort]")) {
  control.addEventListener("click", () => {
    state.sort = control.dataset.sort;
    state.projectOrder = [];
    render();
  });
}

const enrollment = document.querySelector("#enrollment");
const watchToggle = document.querySelector("#watch-toggle");
const projectCombobox = document.querySelector(".project-combobox");
const projectInput = document.querySelector("#project-path");
watchToggle.addEventListener("click", () => {
  const opening = enrollment.hidden;
  enrollment.hidden = !opening;
  watchToggle.setAttribute("aria-expanded", String(opening));
  watchToggle.textContent = opening ? "Close enrollment" : "Watch project";
  if (opening) {
    projectInput.focus();
    if (!state.candidatesLoading) loadProjectCandidates();
  } else {
    closeProjectOptions();
  }
});

projectInput.addEventListener("focus", () => {
  if (enrollment.hidden) return;
  if (!state.candidates.length && !state.candidatesLoading) {
    loadProjectCandidates();
  } else {
    renderProjectOptions();
  }
});

projectInput.addEventListener("input", () => {
  state.candidateActiveIndex = -1;
  renderProjectOptions();
});

projectInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeProjectOptions();
    return;
  }
  if (!["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
  const matches = candidateMatches();
  if (!matches.length) return;
  if (event.key === "Enter" && state.candidateActiveIndex < 0) return;
  event.preventDefault();
  if (event.key === "Enter") {
    chooseCandidate(matches[state.candidateActiveIndex]);
    return;
  }
  const direction = event.key === "ArrowDown" ? 1 : -1;
  const start = state.candidateActiveIndex < 0
    ? (direction > 0 ? -1 : 0)
    : state.candidateActiveIndex;
  state.candidateActiveIndex = (start + direction + matches.length) % matches.length;
  renderProjectOptions();
});

document.addEventListener("pointerdown", (event) => {
  if (!projectCombobox.contains(event.target)) closeProjectOptions();
});

document.querySelector("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#project-path");
  const button = event.submitter || event.currentTarget.querySelector("button[type=submit]");
  try {
    button.disabled = true;
    button.textContent = "Building baseline";
    state.data = await post("/api/projects", { path: input.value });
    state.view = "all";
    state.search = "";
    document.querySelector("#search").value = "";
    const addedPath = input.value.trim();
    input.value = "";
    const added = state.data.projects.find((project) => project.resolved_path === addedPath || project.display_path === addedPath);
    state.candidates = state.candidates.filter(
      (candidate) => candidate.project_id !== added?.project_id,
    );
    closeProjectOptions();
    enrollment.hidden = true;
    watchToggle.setAttribute("aria-expanded", "false");
    watchToggle.textContent = "Watch project";
    if (added) state.selectedProjectId = added.project_id;
    toast("Project baseline committed to the watchlist");
    render();
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
    button.textContent = "Begin watching";
  }
});

syncViewsDisclosure();
refresh();
window.setInterval(refresh, 3000);
