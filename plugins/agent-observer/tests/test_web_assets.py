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
                "search",
            }.issubset(parser.ids)
        )

    def test_dashboard_uses_text_dom_operations_and_only_real_mutations(self):
        script = (ASSETS / "app.js").read_text(encoding="utf-8")
        for unsafe_operation in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(unsafe_operation, script)
        self.assertEqual(
            set(re.findall(r'post\("([^\"]+)"', script)),
            {"/api/projects", "/api/rescan", "/api/findings/seen"},
        )
        self.assertIn("const renderMarkdown =", script)
        self.assertIn('el("button", "queue-dismiss")', script)
        self.assertIn("session?.title?.trim()", script)
        self.assertIn("const latestProjectSession =", script)
        self.assertIn("session.last_activity_at", script)
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


if __name__ == "__main__":
    unittest.main()
