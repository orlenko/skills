# Orchestras & Pairs in Herdr

This sidebar groups visible participating terminals by Orchestra or Pair across
machines. Each member is one line: name, role and machine (`local` for this
computer). A Pair member is named by its terminal's title, such as a renamed
Claude session, because a Pair's own name is the host name; an Orchestra member
keeps its nick. Hover a row for the rest. Click a member to focus his pane; a
terminal without a pane opens in Herdr's peek view. A terminal can appear in
both kinds of group, or in several relationships of one kind.

Groups read `Pair · 2` or `Orchestra · 5`. Hover a group heading and click the
pen to give it a name; Enter saves, an empty name restores the default, and Esc
or clicking away cancels. Names live in this Herdr app's shared state, not in
the Pair or Orchestra. The chevron on the `Orchestras & Pairs` heading folds the
whole section down to that heading.

The panel is a navigation view of terminals already available to your Herdr app,
not a roster. Missing rows can mean a machine disconnected or a publication lease
expired. Activity is Herdr's terminal status, not collaboration presence. Pair
and Orchestra continue to own messaging, membership and authorization.

## Enable publication on each participant

Display publication is off by default. On each participating agent host, enable
it for the owning session with its plugin's `herdr-display enable` command. For
new sessions, you can instead start the agent with `ORLENKO_HERDR_DISPLAY=1` in
its environment. Disable it with `herdr-display disable`; an owning lifecycle
action with `ORLENKO_HERDR_DISPLAY=0` also disables it. Run commands from the
session that owns the relationship, using that plugin's normal selection flags.

**Visibility:** published names, relationship ids, participant ids, role, terminal
incarnation and lease times are readable by every authorized client of that
Herdr core. Enable this only where that audience is appropriate. No invite,
credential, message body, process argument or workdir is published. Installing
the viewing mod does not enable publication or enroll any session.

Existing private enablement survives monitor restart and normal account adoption.
Labels are leases: if an owner exits or a monitor cannot refresh, its row expires
within two minutes, plus the sidebar's 15-second expiry refresh. Herdr must already
have the participating machines connected; the mod opens no network connections.

## Install on the viewing machine

Install on the machine running the Herdr **app**—for example, your laptop—even
when every agent runs elsewhere. From this checkout:

```sh
python3 integrations/herdr/install.py --home "$HOME/.config/herdr-app"
```

`--home` is required; custom Herdr homes are supported. The installer adds only
`mods/collaborations/`. It does not edit layouts, shipped mods, terminal labels,
agent configuration or machine connections.

Herdr docks a newly discovered mod on the left only in a personal layout. Its
shipped layouts (`main`, the default, plus `win98` and `terminals`) are read-only
and skip the mod. On a shipped layout, either open the mod as a pane or tab:

```sh
herdr-app do pane.split --direction right --mod collaborations
herdr-app do tab.new --mod collaborations
```

or clone the layout so new mods dock automatically:

```sh
herdr-app do layout.clone '{"name":"main","as":"mine"}'
```

To put it in the left sidebar between Spaces and Agents, clone `main` as above,
then add this block to the `sidebar-lists` column in `layouts/mine.json`, after
the `spaces` block. A `min` of 41 lets the section fold to its heading.

```json
{ "id": "collaborations", "type": "block", "size": { "weight": 1 }, "min": 41,
  "content": { "mod": "collaborations" } }
```

Herdr's layout controls can move or hide the panel afterwards. The installer refuses to
overwrite locally edited managed files. Repeat it to update an unedited install;
unrelated files are preserved. To customize, copy the mod under another name.

Validate against the **installed app's** current API before using it:

```sh
herdr-app check "$HOME/.config/herdr-app/mods/collaborations"
```

Then try an Orchestra spanning two connected machines, a Pair, and a terminal
without a pane. Check that clicks focus the expected session and that a disconnected
machine removes its rows. The code follows Herdr's published `terminals()`,
`machines()`, `panes()`, `pane.focus` and `terminal.peek` interfaces. Automated Node
tests exercise model, rendering and navigation with synthetic data. A native app
check and rendered smoke are separate checks; they require a Herdr app binary.

## Data contract

Each display publication has a disjoint label key of the form
`orlenko.orchestra.<participant-hash>.<publication-token>` or `orlenko.pair.…`.
Values are version-1 JSON with group/participant ids, display names, role, owner
pid, terminal `created_ms`, issue time and expiry. The mod rejects oversized,
malformed, unsupported-version, expired, future-dated and wrong-incarnation data.
Display strings are bounded and stripped of controls and directional overrides.

Labels are untrusted display claims. Navigation targets always come from Herdr's
current qualified terminal address, checked again at click time. No label can
request shell execution or a message send. The mod reads no collaboration state,
hub credentials, transcripts or clipboard, and uses no `exec` or `fetch`.

UI API reference: [Herdr app guide](https://github.com/herdrdev/herdr-app/blob/master/defaults/AGENTS.md).
