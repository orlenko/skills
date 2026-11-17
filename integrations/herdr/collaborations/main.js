import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { terminals, panes, machines, run, theme } from "herdr";
import { collaborationGroups, navigationFor } from "./model.js";

const ACTIVITY = { working: "Working", blocked: "Needs you", error: "Error", done: "Done", idle: "Idle" };

export default class Collaborations extends View {
  init(_props, cx) {
    this.folded = new Set();
    // Native terminal changes already redraw the mod. This only expires leases.
    cx.timer.every(15000, (cx) => cx.notify());
  }

  activate(row) {
    const target = navigationFor(row, terminals(), panes(), machines());
    if (target) run(target.command, target.args);
  }

  member(row, colors) {
    return v_flex().id(`member-${row.key}`).px(10).py(6).gap(3).rounded(6)
      .hover((s) => s.bg(colors.hover))
      .on_click(() => this.activate(row))
      .child(h_flex().gap(6).items_center()
        .child(div().flex_1().min_w(0).truncate().text_color(colors.foreground).child(row.name))
        .child(div().text_size(10).text_color(colors[row.status] ?? colors.dim).child(ACTIVITY[row.status] ?? "No activity")))
      .child(h_flex().gap(6).items_center().text_size(10.5).text_color(colors.dim)
        .child(div().child(row.role))
        .child(div().child("·"))
        .child(div().truncate().child(row.machine))
        .child(div().flex_1())
        .child(div().child(row.address)));
  }

  group(group, colors) {
    const folded = this.folded.has(group.key);
    const kind = group.kind === "orchestra" ? "Orchestra" : "Pair";
    return v_flex().id(`group-${group.key}`).gap(2)
      .child(h_flex().id(`toggle-${group.key}`).px(8).py(7).gap(6).rounded(6).items_center()
        .hover((s) => s.bg(colors.hover))
        .on_click((_event, cx) => {
          if (this.folded.has(group.key)) this.folded.delete(group.key); else this.folded.add(group.key);
          cx.notify();
        })
        .child(div().text_color(colors.dim).child(folded ? "▸" : "▾"))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(10).text_color(colors.dim).child(`${kind} · ${group.rows.length}`))
          .child(div().truncate().font_semibold().child(group.name)))
        .child(div().text_size(9).text_color(colors.dim).child(group.id.slice(-8))))
      .children(folded ? [] : group.rows.map((row) => this.member(row, colors)));
  }

  render() {
    const colors = theme();
    const groups = collaborationGroups(terminals(), machines());
    return v_flex().id("collaborations-root").size_full().min_h(0).text_size(12).text_color(colors.foreground)
      .child(v_flex().px(12).pt(12).pb(8).gap(4)
        .child(div().font_semibold().child("Orchestras & Pairs"))
        .child(div().text_size(10.5).text_color(colors.dim).child("Visible terminals across machines")))
      .child(v_flex().id("collaboration-list").flex_1().min_h(0).overflow_y_scrollbar().px(4).gap(6)
        .children(groups.map((group) => this.group(group, colors)))
        .children(groups.length ? [] : [v_flex().px(8).py(12).gap(6).text_color(colors.dim)
          .child(div().child("No published collaborations"))
          .child(div().text_size(10.5).child("Enable Herdr display in each participating session."))]))
      .child(div().px(12).py(9).text_size(10).text_color(colors.dim)
        .child("Activity is terminal status. Missing terminals may be disconnected; this is not a full roster."));
  }
}
