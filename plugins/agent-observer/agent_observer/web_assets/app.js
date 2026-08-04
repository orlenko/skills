"use strict";

const state = {
  data: null,
  view: "all",
  search: "",
  busy: false,
  selectedProjectId: null,
  projectOrder: [],
  focusAfterRender: null,
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

const findingPriorities = {
  decision_requested: 0,
  turn_aborted: 1,
  turn_completed: 2,
  no_completion_observed: 3,
};

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

const unseenFindings = (project) => project.findings
  .filter((finding) => !finding.seen)
  .sort((left, right) => {
    const leftPriority = findingPriorities[left.kind] ?? 9;
    const rightPriority = findingPriorities[right.kind] ?? 9;
    return leftPriority - rightPriority || (right.updated_at || 0) - (left.updated_at || 0);
  });

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

const markFindingSeen = async (finding, project, control) => {
  try {
    control.disabled = true;
    state.data = await post("/api/findings/seen", { finding_id: finding.finding_id });
    state.focusAfterRender = `dismiss:${project.project_id}`;
    toast("Attention item marked seen locally");
    render({ forceSort: true });
  } catch (error) {
    showError(error);
  } finally {
    control.disabled = false;
  }
};

const projectSignal = (project) => {
  const finding = unseenFindings(project)[0];
  if (finding) {
    return {
      kind: "observed",
      label: "Observed",
      summary: plainPreview(finding.summary),
      source: `${finding.provider} · ${finding.session_id.slice(0, 8)}`,
      finding,
    };
  }
  if (project.facets.prominent_review_items > 0 && project.review) {
    return {
      kind: "model",
      label: "Model review",
      summary: plainPreview(project.review.summary || "A bounded review suggests another look."),
      source: `${project.review.analyzer_provider} analysis · ${project.review.status}`,
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
  const finding = unseenFindings(project)[0];
  if (finding) return [findingPriorities[finding.kind] ?? 8, project.facets.activity_age_seconds ?? Infinity];
  if (project.facets.prominent_review_items > 0) return [10, project.facets.activity_age_seconds ?? Infinity];
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
      ...project.findings.map((finding) => finding.summary),
      project.review?.summary || "",
      ...project.sessions.flatMap((session) => [session.provider, session.session_id, session.title || ""]),
    ].join(" ").toLocaleLowerCase();
    return haystack.includes(query);
  });
};

const orderedProjects = (freezeOrder) => {
  const projects = filteredProjects();
  const sorted = [...projects].sort((left, right) => {
    const [leftRank, leftAge] = sortKey(left);
    const [rightRank, rightAge] = sortKey(right);
    return leftRank - rightRank || leftAge - rightAge || left.display_path.localeCompare(right.display_path);
  });
  if (freezeOrder && state.projectOrder.length) {
    const byId = new Map(projects.map((project) => [project.project_id, project]));
    const stable = state.projectOrder.map((id) => byId.get(id)).filter(Boolean);
    const present = new Set(stable.map((project) => project.project_id));
    stable.push(...sorted.filter((project) => !present.has(project.project_id)));
    state.projectOrder = stable.map((project) => project.project_id);
    return stable;
  }
  state.projectOrder = sorted.map((project) => project.project_id);
  return sorted;
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
      render({ forceSort: true });
    });
    nav.append(button);
  }
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
  if (signal.kind === "observed" && project.facets.prominent_review_items > 0) {
    claimMeta.append(badge("Also model review", "truth-model"));
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
  if (signal.finding) {
    const dismiss = el("button", "queue-dismiss");
    dismiss.type = "button";
    dismiss.dataset.focusKey = `dismiss:${project.project_id}`;
    dismiss.setAttribute("aria-label", `Mark seen: ${plainPreview(signal.finding.summary)}`);
    dismiss.title = "Mark this attention item seen locally";
    dismiss.append(el("span", "checkbox-mark", ""), el("span", "", "Seen"));
    dismiss.addEventListener("click", () => markFindingSeen(signal.finding, project, dismiss));
    row.append(dismiss);
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

const renderFinding = (finding, project) => {
  const row = el("article", `finding ${finding.seen ? "seen" : "unseen"}`);
  const heading = el("div", "item-heading");
  heading.append(el("h4", "", humanLabel(finding.kind)));
  heading.append(badge(finding.seen ? "Seen locally" : "Unseen", finding.seen ? "local-state" : "truth-observed"));
  row.append(heading, renderMarkdown(finding.summary, "item-summary"));
  const evidence = finding.details?.evidence_excerpt;
  if (evidence) {
    const quote = el("blockquote", "evidence-quote");
    quote.append(renderMarkdown(evidence, "evidence-markdown"));
    row.append(quote);
  }
  row.append(el(
    "p",
    "provenance",
    `${finding.provider} · ${finding.session_id.slice(0, 8)} · ${absoluteTime(finding.updated_at)}`,
  ));

  const structured = finding.details?.items;
  const items = Array.isArray(structured)
    ? structured
    : (structured && typeof structured === "object" ? Object.values(structured) : []);
  if (items.length) {
    const list = el("div", "structured-questions");
    for (const item of items) {
      const question = el("div", "structured-question");
      const questionHead = el("p", "question-heading");
      questionHead.append(el("strong", "", item.question || "Question"));
      questionHead.append(badge(humanLabel(item.state || "unknown"), "local-state"));
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

  if (!finding.seen) {
    const seen = el("button", "quiet-button", "Mark seen locally");
    seen.type = "button";
    seen.dataset.focusKey = `seen:${finding.finding_id}`;
    seen.addEventListener("click", () => markFindingSeen(finding, project, seen));
    row.append(seen);
  }
  return row;
};

const renderReview = (review) => {
  const section = el("section", "inspector-section model-review");
  section.append(sectionHeading("Conversation review", "Model review", "truth-model"));
  if (!review) {
    section.append(el("p", "empty-copy", "No interactive review has been published for this project."));
    return section;
  }
  section.append(renderMarkdown(review.summary || "Review prepared without a summary.", "review-summary"));
  const model = review.analyzer_model ? ` · ${review.analyzer_model}` : "";
  section.append(el(
    "p",
    "provenance",
    `${review.analyzer_provider}${model} · interactive analysis · ${humanLabel(review.status)}`,
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
  for (const item of review.items || []) {
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
    section.append(row);
  }
  for (const limitation of review.limitations || []) {
    section.append(el("p", "coverage", `Limit: ${limitation}`));
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
  if (identity.secondary.length) {
    header.append(el("p", "inspector-identifiers", identity.secondary.join(" · ")));
  }
  const resolved = el("code", "resolved-path selectable", project.resolved_path);
  header.append(resolved);
  const actions = el("div", "inspector-actions");
  const copyPath = el("button", "quiet-button", "Copy path");
  copyPath.type = "button";
  copyPath.dataset.focusKey = `copy-path:${project.project_id}`;
  copyPath.addEventListener("click", () => copyText(project.resolved_path, "Project path copied"));
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
  actions.append(copyPath, rescan);
  header.append(actions);
  inspector.append(header, renderFacets(project));

  const findings = el("section", "inspector-section");
  findings.append(sectionHeading("Source-backed attention", "Observed", "truth-observed"));
  const ordered = [...project.findings].sort((left, right) => Number(left.seen) - Number(right.seen) || (right.updated_at || 0) - (left.updated_at || 0));
  if (ordered.length) ordered.forEach((finding) => findings.append(renderFinding(finding, project)));
  else findings.append(el("p", "empty-copy", "No factual findings have been recorded for this project."));
  inspector.append(findings, renderReview(project.review), renderSessions(project), renderSources(project), renderChanges(project));
};

const renderServices = () => {
  const notices = document.querySelector("#service-notices");
  notices.replaceChildren();
  const service = document.querySelector("#service-state");
  const server = state.data?.services?.server;
  const daemon = state.data?.services?.daemon;
  const heartbeatAge = daemon?.heartbeat_at && state.data?.generated_at
    ? Math.max(0, state.data.generated_at - daemon.heartbeat_at)
    : null;
  const heartbeatStale = daemon?.running && (heartbeatAge === null || heartbeatAge > 15);
  const healthy = Boolean(server?.running && daemon?.running && !heartbeatStale && !daemon?.error);
  service.className = `service-state ${healthy ? "healthy" : "issue"}`;
  service.lastElementChild.textContent = healthy
    ? "Collector and dashboard live"
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
};

const findFocusTarget = (key) => {
  if (!key) return null;
  return [...document.querySelectorAll("[data-focus-key]")]
    .find((node) => node.dataset.focusKey === key) || null;
};

const render = ({ forceSort = false } = {}) => {
  const active = document.activeElement;
  const focusKey = active?.dataset?.focusKey;
  const list = document.querySelector("#projects");
  const inspector = document.querySelector("#inspector");
  const freezeOrder = !forceSort && Boolean(
    list.matches(":hover") || list.contains(active) || inspector.contains(active),
  );
  const listScroll = list.scrollTop;
  const inspectorScroll = inspector.scrollTop;

  renderViews();
  const projects = orderedProjects(freezeOrder);
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
  render({ forceSort: true });
});

const enrollment = document.querySelector("#enrollment");
const watchToggle = document.querySelector("#watch-toggle");
watchToggle.addEventListener("click", () => {
  const opening = enrollment.hidden;
  enrollment.hidden = !opening;
  watchToggle.setAttribute("aria-expanded", String(opening));
  watchToggle.textContent = opening ? "Close enrollment" : "Watch project";
  if (opening) document.querySelector("#project-path").focus();
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
    const addedPath = input.value;
    input.value = "";
    enrollment.hidden = true;
    watchToggle.setAttribute("aria-expanded", "false");
    watchToggle.textContent = "Watch project";
    const added = state.data.projects.find((project) => project.resolved_path === addedPath || project.display_path === addedPath);
    if (added) state.selectedProjectId = added.project_id;
    toast("Project baseline committed to the watchlist");
    render({ forceSort: true });
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
    button.textContent = "Begin watching";
  }
});

refresh();
window.setInterval(refresh, 3000);
