from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ASSETS = PACKAGE_ROOT / "agent_observer" / "web_assets"


class _DocumentInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.labels_for: set[str] = set()
        self.controls: set[str] = set()
        self.inline_handlers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if identifier := values.get("id"):
            self.ids.append(identifier)
            if tag in {"input", "select", "textarea"}:
                self.controls.add(identifier)
        if tag == "label" and values.get("for"):
            self.labels_for.add(str(values["for"]))
        self.inline_handlers.extend(name for name, _value in attrs if name.startswith("on"))


class WebAssetTests(unittest.TestCase):
    def test_dashboard_document_has_stable_unique_landmarks_and_labels(self):
        parser = _DocumentInventory()
        parser.feed((ASSETS / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertEqual(parser.controls - parser.labels_for, set())
        self.assertEqual(parser.inline_handlers, [])
        self.assertTrue(
            {
                "views",
                "projects",
                "inspector",
                "service-notices",
                "add-form",
                "project-options",
                "search",
                "views-toggle",
            }.issubset(parser.ids)
        )

    def test_dashboard_uses_text_dom_operations_and_only_real_mutations(self):
        script = (ASSETS / "app.js").read_text(encoding="utf-8")
        for unsafe_operation in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(unsafe_operation, script)
        self.assertEqual(
            set(re.findall(r'post\("([^\"]+)"', script)),
            {
                "/api/projects",
                "/api/projects/dismiss-attention",
                "/api/projects/remove",
                "/api/rescan",
            },
        )
        self.assertIn("const renderMarkdown =", script)
        self.assertIn("openDetails: new Set()", script)
        self.assertIn("const persistentDetails =", script)
        self.assertIn('node.addEventListener("toggle"', script)
        self.assertIn("review-notes:${project.project_id}", script)
        self.assertIn('window.matchMedia("(max-width: 74rem)")', script)
        self.assertIn('window.localStorage.getItem(viewsPreferenceKey)', script)
        self.assertIn("const syncViewsDisclosure =", script)
        self.assertIn("setViewsCollapsed(!state.viewsCollapsed)", script)
        self.assertIn('el("button", "queue-dismiss")', script)
        self.assertIn("const renderProjectOptions =", script)
        self.assertIn('fetch("/api/project-candidates"', script)
        self.assertIn("candidate.session?.topic", script)
        self.assertIn("candidate.branch", script)
        self.assertIn("candidate.resolved_path", script)
        self.assertIn("const renderSortControls =", script)
        self.assertIn('state.sort === "activity"', script)
        self.assertIn('state.sort === "project"', script)
        self.assertIn("Dismiss current attention", script)
        self.assertIn("Needs your input", script)
        self.assertIn("Activity history and diagnostics", script)
        self.assertIn("session?.title?.trim()", script)
        self.assertIn("const latestProjectSession =", script)
        self.assertIn("const projectRemovalButton =", script)
        self.assertIn('projectRemovalButton(project, "row", "Remove")', script)
        self.assertIn('projectRemovalButton(project, "inspector", "Stop watching")', script)
        self.assertIn("session.last_activity_at", script)
        self.assertIn("state.data?.analyzer", script)
        self.assertIn("Semantic analyzer is detached", script)
        self.assertIn("project.node?.display_name", script)
        self.assertIn('project.origin === "remote"', script)
        self.assertIn("Full context and watchlist controls remain on", script)
        self.assertNotIn('"Copy path"', script)
        self.assertIn("state.data?.remote_nodes", script)
        self.assertIn('["connected", "revoked"].includes(node.transport_state)', script)
        self.assertIn("state.data?.services?.analyzer", script)
        self.assertIn("activity-gated review", script)
        self.assertNotIn("const signalSession =", script)
        for speculative_control in (
            "Snooze",
            "Still relevant",
            "Send to worker",
            "Open evidence dialog",
        ):
            self.assertNotIn(speculative_control, script)

    def test_dashboard_assets_do_not_load_remote_resources(self):
        for name in ("index.html", "app.js", "styles.css"):
            contents = (ASSETS / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"https?://", contents), name)

    def test_attention_palette_is_cool_and_red_remains_semantic(self):
        styles = (ASSETS / "styles.css").read_text(encoding="utf-8")
        self.assertIn("--surface-selected: oklch(93.6% 0.027 230);", styles)
        self.assertIn("--accent: oklch(47% 0.105 238);", styles)
        self.assertIn("--model: oklch(43% 0.09 178);", styles)
        self.assertIn("--danger: oklch(44% 0.15 27);", styles)
        self.assertNotIn("--accent: oklch(48% 0.12 43);", styles)

    def test_narrow_layout_can_release_the_navigation_column(self):
        styles = (ASSETS / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".shell.views-collapsed { grid-template-columns: minmax(0, 1fr); }", styles)


if __name__ == "__main__":
    unittest.main()
