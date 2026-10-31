"""Execute the bundled page with real JavaScript and in-memory DOM sinks.

Node is already on the hosted CI runners; no browser or npm packages are needed.
The sinks collect rendered HTML, but do not model browser parsing or layout.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_orchestra import podium  # noqa: E402


NODE = shutil.which("node")
RENDER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {page, hub} = JSON.parse(fs.readFileSync(0, 'utf8'));

// Only the DOM operations used by this fixture. Keep the production script
// intact, including startup, fetch, and its render-error handler.
class Element {
  constructor() { this.html = ''; this.children = []; this.textContent = ''; }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() {
    return this.html + this.children.map(child => child.innerHTML).join('');
  }
  get firstElementChild() { return this; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.html = ''; this.children = children; }
  querySelectorAll() { return []; }
}
const elements = new Map(['app', 'age', 'live-dot', 'live-label']
  .map(id => [id, new Element()]));
const feeds = {
  'orchestra.json': hub,
  'work-status.json': {
    summary: 'Runtime regression fixture', members: [],
    updated_at: new Date().toISOString()
  }
};
let poll;
const context = vm.createContext({
  document: {
    createElement: () => new Element(),
    getElementById: id => {
      if (!elements.has(id)) throw Error('Unexpected element: ' + id);
      return elements.get(id);
    }
  },
  fetch: async path => {
    const body = feeds[path.split('?')[0]];
    return {status: body ? 200 : 404, ok: !!body, json: async () => body};
  },
  setInterval: (callback, ms) => { poll = callback; },
  URL
});
(async () => {
  vm.runInContext(page.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  // Drain the actual startup render, then exercise the periodic refresh too.
  await new Promise(resolve => setImmediate(resolve));
  await poll();
  process.stdout.write(JSON.stringify({
    html: elements.get('app').innerHTML,
    age: elements.get('age').textContent,
    live: elements.get('live-label').textContent
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@unittest.skipUnless(NODE, "Node is needed for the Podium JavaScript regression (required in CI)")
class RenderTest(unittest.TestCase):
    def test_roster_nicknames_and_task_owners_render_without_crashing(self) -> None:
        hub = {
            "monitor_alive": True,
            "roster": {"age": 0, "members": [
                {"id": "mb_aiq", "name": "aiq", "nicks": ["Harp"], "presence": "connected"},
                {"id": "mb_player", "name": "Tsymbaly", "presence": "stale"},
                {"id": "mb_fallback", "nicks": ["Oboe", '<Reed & "Co">']},
            ]},
            "tasks": {
                "age": 0, "lifecycle": "derived", "closed_count": 0,
                "open": [{"task": "t_render", "state": "blocked", "recent": True,
                          "owners": [{"id": "mb_aiq", "state": "blocked"}]}],
                "attention": [{"task": "t_render", "owner": "mb_aiq", "kind": "blocked"}],
            },
        }
        rendered = subprocess.run(
            [NODE, "-e", RENDER], input=json.dumps({"page": podium.PAGE.read_text(), "hub": hub}),
            text=True, capture_output=True, timeout=10, check=True,
        )
        result = json.loads(rendered.stdout)
        self.assertNotIn("Podium could not render:", result["age"])
        self.assertEqual(result["live"], "live")
        html = result["html"]
        self.assertIn("Members <", html)
        self.assertIn('<span class="seat">aiq (Harp)</span>', html)
        self.assertIn('<span class="seat">Tsymbaly</span>', html)
        self.assertIn('mb_fallback (Oboe, &lt;Reed &amp; &quot;Co&quot;&gt;)', html)
        for presence in ("connected", "stale", "unknown"):
            self.assertIn(f'<span class="meta">{presence}</span>', html)
        self.assertIn("Open tasks <", html)
        self.assertIn('title="aiq (Harp): blocked"', html)
        # The owner is named in both the task row and the attention section.
        self.assertEqual(html.count('<span class="seat">aiq (Harp)</span>'), 2)


if __name__ == "__main__":
    unittest.main()
