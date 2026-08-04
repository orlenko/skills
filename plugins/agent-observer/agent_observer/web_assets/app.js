"use strict";

const state = {
  data: null,
  view: "all",
  search: "",
  busy: false,
  projectOpen: new Map(),
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

const relativeAge = (seconds) => {
  if (seconds === null || seconds === undefined) return "activity unknown";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
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

const badge = (text, kind = "") => el("span", `badge ${kind}`.trim(), text);

const renderViews = () => {
  const nav = document.querySelector("#views");
  nav.replaceChildren();
  for (const [key, label] of viewDefinitions) {
    const button = el("button", "view-button");
    button.type = "button";
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

const renderFinding = (finding, project) => {
  const row = el("article", "finding");
  const heading = el("h3", "", finding.kind.replaceAll("_", " "));
  row.append(heading, el("p", "", finding.summary));
  const meta = el("p", "evidence", `Observed in ${finding.provider}:${finding.session_id.slice(0, 8)}`);
  row.append(meta);
  if (!finding.seen) {
    const actions = el("div", "row-actions");
    const seen = el("button", "", "Mark seen");
    seen.type = "button";
    seen.addEventListener("click", async () => {
      try {
        state.data = await post("/api/findings/seen", { finding_id: finding.finding_id });
        toast("Finding marked seen");
        render();
      } catch (error) { showError(error); }
    });
    actions.append(seen);
    row.append(actions);
  }
  return row;
};

const renderReview = (review) => {
  const section = el("section", "review");
  const label = el("p", "section-label meta", "MODEL REVIEW");
  section.append(label);
  if (!review) {
    section.append(el("p", "evidence", "No interactive review has been published yet."));
    return section;
  }
  section.append(el("p", "", review.summary || "Review prepared, awaiting analyzer output."));
  const model = review.analyzer_model ? `, ${review.analyzer_model}` : "";
  section.append(el("p", "evidence", `${review.analyzer_provider}${model} · ${review.status}`));
  if (review.target_session) {
    const target = `${review.target_session.provider}:${review.target_session.session_id.slice(0, 8)}`;
    const count = review.coverage?.message_count ?? 0;
    const limit = review.coverage?.message_limit ?? 40;
    section.append(el("p", "evidence", `Reviewed ${target} · ${count}/${limit} visible messages`));
  }
  for (const gap of review.coverage?.gaps || []) {
    section.append(el("p", "evidence issue-text", `Coverage gap: ${gap}`));
  }
  for (const item of review.items || []) {
    const row = el("article", "review-item");
    row.append(el("h3", "", item.title));
    row.append(el("p", "", item.detail));
    row.append(el("p", "evidence", `“${item.evidence_excerpt}”`));
    row.append(el("p", "evidence", `${item.type} · ${item.assessment} · ${item.session_id.slice(0, 8)}`));
    section.append(row);
  }
  for (const limitation of review.limitations || []) {
    section.append(el("p", "evidence", `Limit: ${limitation}`));
  }
  return section;
};

const renderProject = (project) => {
  const details = el("details", "project");
  const savedOpen = state.projectOpen.get(project.project_id);
  details.open = savedOpen ?? Boolean(project.primary_finding || project.facets.prominent_review_items);
  details.addEventListener("toggle", () => {
    state.projectOpen.set(project.project_id, details.open);
  });
  const summary = el("summary");

  const identity = el("div");
  const pathLine = el("div", "path-line");
  pathLine.append(el("span", "chevron", "›"), el("span", "path", project.display_path));
  identity.append(pathLine);
  const badges = el("div", "badges");
  const providers = [...new Set(project.sessions.map((session) => session.provider))];
  providers.forEach((provider) => badges.append(badge(provider)));
  if (project.current_branch) badges.append(badge(project.current_branch));
  if (project.primary_finding) badges.append(badge("observed", "observed"));
  if (project.facets.prominent_review_items) badges.append(badge("model review", "model"));
  if (project.facets.health !== "healthy") badges.append(badge(project.facets.health, "issue"));
  identity.append(badges);

  const headline = el("div", "headline");
  const primary = project.primary_finding?.summary || project.review?.summary || "No current finding";
  headline.append(el("strong", "", primary));
  headline.append(el("span", "", `${project.sessions.length} known session${project.sessions.length === 1 ? "" : "s"}`));

  const age = el("div", "age");
  age.append(el("strong", "", relativeAge(project.facets.activity_age_seconds)));
  age.append(el("span", "", project.facets.activity));
  summary.append(identity, headline, age);
  details.append(summary);

  const body = el("div", "project-body");
  const primaryColumn = el("div");
  const factual = el("section");
  factual.append(el("p", "section-label meta", "SOURCE-BACKED ATTENTION"));
  const unseen = project.findings.filter((finding) => !finding.seen);
  if (unseen.length) unseen.slice(0, 5).forEach((finding) => factual.append(renderFinding(finding, project)));
  else factual.append(el("p", "evidence", "No unseen factual findings."));
  primaryColumn.append(factual, renderReview(project.review));

  const side = el("aside");
  side.append(el("p", "section-label meta", "SESSIONS"));
  for (const session of project.sessions.slice(0, 20)) {
    const row = el("div", "session-row");
    const left = el("span");
    left.append(el("strong", "", session.provider), document.createTextNode(" "));
    left.append(el("code", "", session.session_id.slice(0, 8)));
    row.append(left, el("span", "", relativeAge(session.activity_age_seconds)));
    side.append(row);
  }
  const actions = el("div", "project-actions");
  const rescan = el("button", "", "Rescan sources");
  rescan.type = "button";
  rescan.addEventListener("click", async () => {
    try {
      rescan.disabled = true;
      state.data = await post("/api/rescan", { project: project.project_id });
      toast("Source discovery complete");
      render();
    } catch (error) { showError(error); }
    finally { rescan.disabled = false; }
  });
  actions.append(rescan);
  side.append(actions);
  body.append(primaryColumn, side);
  details.append(body);
  return details;
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
      ...project.sessions.flatMap((session) => [session.provider, session.session_id, session.title || ""]),
    ].join(" ").toLocaleLowerCase();
    return haystack.includes(query);
  });
};

const render = () => {
  renderViews();
  const list = document.querySelector("#projects");
  list.replaceChildren();
  list.setAttribute("aria-busy", "false");
  const projects = filteredProjects();
  if (!projects.length) {
    const empty = el("div", "empty");
    empty.append(el("strong", "", state.data?.projects.length ? "Nothing in this view" : "No projects watched yet"));
    empty.append(document.createTextNode(state.data?.projects.length
      ? "Try another view or clear the path filter."
      : "Add a project path above, or invoke the Observer skill from a project session."));
    list.append(empty);
  } else {
    projects.forEach((project) => list.append(renderProject(project)));
  }
  const view = viewDefinitions.find(([key]) => key === state.view);
  document.querySelector("#view-kicker").textContent = (view?.[1] || "All projects").toUpperCase();
  if (state.data) {
    document.querySelector("#updated-at").textContent = `updated ${relativeAge(Date.now() / 1000 - state.data.generated_at)}`;
    const service = document.querySelector("#service-state");
    const server = state.data.services?.server?.running;
    const daemon = state.data.services?.daemon?.running;
    service.className = `service-state ${server && daemon ? "healthy" : "issue"}`;
    service.lastChild.textContent = server && daemon ? " Collector and dashboard live" : " Sidecar issue";
  }
};

const showError = (error) => {
  const node = document.querySelector("#error");
  node.textContent = error instanceof Error ? error.message : String(error);
  node.hidden = false;
};

const refresh = async () => {
  if (state.busy) return;
  state.busy = true;
  try {
    const response = await fetch("/api/status", { credentials: "same-origin" });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Refresh failed (${response.status})`);
    state.data = value;
    document.querySelector("#error").hidden = true;
    render();
  } catch (error) { showError(error); }
  finally { state.busy = false; }
};

document.querySelector("#search").addEventListener("input", (event) => {
  state.search = event.target.value;
  render();
});

document.querySelector("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#project-path");
  try {
    const button = event.submitter;
    if (button) button.disabled = true;
    state.data = await post("/api/projects", { path: input.value });
    state.view = "all";
    input.value = "";
    toast("Project added to the watchlist");
    render();
  } catch (error) { showError(error); }
  finally { if (event.submitter) event.submitter.disabled = false; }
});

refresh();
window.setInterval(refresh, 3000);
