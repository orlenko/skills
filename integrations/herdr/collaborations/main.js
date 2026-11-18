import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { terminals, panes, agents, machines, run, theme, shared, setShared, layout } from "herdr";
import { cleanText, collaborationGroups, groupLabel, navigationFor } from "./model.js";

const NAMES = "collaborations.names";
const FOLDED = "collaborations.folded";
const HEADER = 41;
const NOTE = "A navigation view of visible terminals, not a roster: a missing row may be a "
  + "disconnected machine or an expired lease. Activity is terminal status.";
const chevron = (open) => svg(`mods/workspace-chrome/chevron-${open ? "down" : "right"}.svg`).size(11).flex_none();

// Titles by terminal address, for when terminals() records omit them.
function titleMap() {
  const titles = new Map();
  for (const item of [...agents(), ...panes()]) {
    const address = item?.address ?? item?.terminal;
    if (typeof address === "string" && typeof item.title === "string" && item.title && !titles.has(address)) {
      titles.set(address, item.title);
    }
  }
  return titles;
}

function find(node, test) {
  if (!node || typeof node !== "object") return null;
  if (test(node)) return node;
  for (const child of node.children ?? []) {
    const found = find(child, test);
    if (found) return found;
  }
  return null;
}

// This mod's own layout block, when a layout places it (not as a pane or tab).
const ownBlock = () => find(layout(), (n) => n.type === "block" && n.content?.mod === "collaborations" && typeof n.id === "string");
// The sidebar's rail: too narrow for rows.
const narrow = () => (find(layout(), (n) => n.id === "sidebar")?.size?.px ?? Infinity) < 160;

function names() {
  const value = shared(NAMES);
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export default class Collaborations extends View {
  init(_props, cx) {
    this.folded = new Set();
    this.editing = null;
    this.hoveredGroup = null;
    // Native terminal changes already redraw the mod. This only expires leases.
    cx.timer.every(15000, (cx) => cx.notify());
  }

  activate(row) {
    const target = navigationFor(row, terminals(), panes(), machines());
    if (target) run(target.command, target.args);
  }

  // Fold the whole section down to its header, and give the block its size back after.
  toggleSection() {
    const folding = !shared(FOLDED);
    setShared(FOLDED, folding);
    const block = ownBlock();
    if (!block) return;
    if (folding) {
      setShared(`${FOLDED}.size.${block.id}`, block.size ?? null);
      run("layout.resize", { id: block.id, px: HEADER });
    } else {
      const size = shared(`${FOLDED}.size.${block.id}`);
      const back = Number.isFinite(size?.px) && size.px > HEADER ? { px: size.px } : { weight: Number.isFinite(size?.weight) ? size.weight : 1 };
      run("layout.resize", { id: block.id, ...back });
    }
  }

  startRename(group, label, cx) {
    this.editing = group.key;
    this.input = InputState.new({ value: label });
    this.input.set_selected_range({ start: 0, end: label.length });
    this.input.on("submit", (_e, cx) => this.finishRename(group.key, this.input.value(), cx));
    this.input.on("blur", (_e, cx) => this.finishRename(group.key, null, cx));
    this.input.focus();
    cx.notify();
  }

  // Enter saves; an empty name goes back to the default. Leaving the field cancels.
  finishRename(key, value, cx) {
    if (this.editing !== key) return;
    this.editing = null;
    if (value !== null) {
      const next = { ...names() };
      const name = cleanText(value, "", 60);
      if (name) next[key] = name; else delete next[key];
      setShared(NAMES, next);
    }
    cx?.notify();
  }

  status(t, status) {
    if (status === "working") return svg("mods/workspace-chrome/spinner.svg").size(12).mx(1).flex_none().text_color(t.working).spin(900);
    if (status === "blocked" || status === "error") return div().size(14).flex_none().rounded(4).bg(t.blocked + "26").text_size(10.5).font_bold().text_color(t.blocked).flex().items_center().justify_center().child("!");
    if (status === "idle") return div().size(7).mx(3).flex_none().rounded(4).border(1.5).border_color(t.idle ?? t.dim);
    return div().size(7).mx(3).flex_none().rounded(4).bg(status === "done" ? t.done ?? t.dim : t.dim + "00");
  }

  member(row, t) {
    const tip = [row.display, row.title && row.title !== row.display ? row.title : null,
      row.name !== row.display ? row.name : null, `${row.role} on ${row.machine}`, row.status ?? "no activity"]
      .filter(Boolean).join(" · ");
    return h_flex().id(`member-${row.key}`).px(8).h(30).flex_none().rounded(8).items_center().gap(9)
      .cursor_pointer().hover((s) => s.bg(t.hover)).tooltip(tip)
      .on_click(() => this.activate(row))
      .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(row.display))
      .child(div().flex_none().text_size(11).text_color(t.dim).child(row.role))
      .child(div().flex_none().max_w(90).truncate().text_size(10).text_color(t.dim).child(row.machine))
      .child(this.status(t, row.status));
  }

  group(group, t, saved) {
    const folded = this.folded.has(group.key);
    const label = groupLabel(group, saved);
    const kind = group.kind === "orchestra" ? "Orchestra" : "Pair";
    const head = this.editing === group.key
      ? h_flex().id(`rename-${group.key}`).h(26).px(4).flex_none().items_center()
        .on_key_down((e, cx) => { if (e.key === "escape") this.finishRename(group.key, null, cx); })
        .child(Input.new(this.input).flex_1().h(24).px(6).rounded(6).border_1().border_color(t.accent)
          .bg(t.background).text_size(11.5).text_color(t.foreground))
      : h_flex().id(`toggle-${group.key}`).h(26).px(8).gap(7).flex_none().rounded(6).items_center()
        .text_size(11).text_color(t.dim).cursor_pointer().hover((s) => s.bg(t.hover))
        .tooltip(`${kind} ${group.id}`)
        .on_hover((on, cx) => {
          if (on) this.hoveredGroup = group.key;
          else if (this.hoveredGroup === group.key) this.hoveredGroup = null;
          cx.notify();
        })
        .on_click((_e, cx) => {
          if (folded) this.folded.delete(group.key); else this.folded.add(group.key);
          cx.notify();
        })
        .child(div().flex_1().min_w(0).truncate().child(`${label} · ${group.rows.length}`))
        .when(this.hoveredGroup === group.key, (el) => el.child(div().id(`rename-button-${group.key}`).flex_none().p(2).rounded(4)
          .hover((s) => s.bg(t.hover).text_color(t.foreground)).tooltip(`Name this ${kind.toLowerCase()}`)
          .on_click((_e, cx) => { cx.stop_propagation(); this.startRename(group, label, cx); })
          .child(svg("mods/workspace-chrome/square-pen.svg").size(12))))
        .child(chevron(!folded));
    return v_flex().id(`group-${group.key}`).flex_none()
      .child(head)
      .children(folded ? [] : group.rows.map((row) => this.member(row, t)));
  }

  render() {
    const t = theme();
    const root = v_flex().id("collaborations-root").size_full().min_h(0).bg(t.chrome)
      .border_t(1).border_color(t.border).text_size(12.5).text_color(t.foreground);
    if (narrow()) return root;
    const sectionFolded = Boolean(shared(FOLDED));
    const groups = sectionFolded ? [] : collaborationGroups(terminals(), machines(), Date.now() / 1000, titleMap());
    const saved = names();
    return root
      .child(h_flex().id("collaborations-header").px(16).h(HEADER).pt(14).pb(5).flex_none().items_center().gap(7)
        .cursor_pointer().tooltip(NOTE).on_click(() => this.toggleSection())
        .child(div().flex_1().text_size(11.5).font_medium().text_color(t.dim).child("Orchestras & Pairs"))
        .child(div().text_color(t.dim).child(chevron(!sectionFolded))))
      .when(!sectionFolded, (el) => el.child(v_flex().id("collaboration-list").flex_1().min_h(0).overflow_y_scrollbar().px(8).gap(4)
        .children(groups.map((group) => this.group(group, t, saved)))
        .children(groups.length ? [] : [div().px(8).py(6).text_size(12).text_color(t.dim)
          .child("No published collaborations. Enable Herdr display in each participating session.")])));
  }
}
