import os, sys, base64, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sub"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot           # noqa: E402
import subserver as s  # noqa: E402


class TestAdminThemeToggle(unittest.TestCase):
    def test_page_has_theme_scaffold(self):
        page = bot._page("عنوان", "<p>x</p>")
        self.assertIn("data-theme", page)                          # attribute-driven theming
        self.assertIn("[data-theme=dark]", page.replace(" ", ""))  # dark palette block
        self.assertIn("toggleTheme", page)                         # toggle handler
        self.assertIn("mj-theme", page)                            # persisted in localStorage
        self.assertIn("light dark", page)                          # color-scheme adapts

    def test_toggle_button_on_every_page(self):
        self.assertIn("themebtn", bot._top())                      # even with no session
        self.assertIn("themebtn", bot.render_expired())            # and on the expired page
        both = bot._top(csrf="abc")
        self.assertIn("themebtn", both)                            # alongside logout
        self.assertIn("خروج", both)

    def test_chart_bars_follow_theme(self):
        # svg bars must not be hardcoded black — invisible on a dark card
        svg = bot.svg_bars([("2026-07-01", 5), ("2026-07-02", 0)])
        self.assertIn("currentColor", svg)
        self.assertNotIn('fill="#111111"', svg)


class TestSubscriberThemeToggle(unittest.TestCase):
    def _page(self):
        b64 = base64.b64encode(
            b"vless://11111111-1111-1111-1111-111111111111@1.2.3.4:443?type=ws#N").decode()
        info = {"used_bytes": 100, "limit_bytes": 0, "expiry_ts": 0, "created_ts": 0}
        st, ctype, body, extra = s.build_response("u_x", b64, info, "Mozilla/5.0", False)
        return body.decode("utf-8")

    def test_page_has_theme_scaffold(self):
        page = self._page()
        self.assertIn("themebtn", page)                            # icon-only toggle button
        self.assertIn("data-theme", page)
        self.assertIn("[data-theme=dark]", page.replace(" ", ""))
        self.assertIn("toggleTheme", page)
        self.assertIn("mj-theme", page)

    def test_unlimited_bars_follow_theme(self):
        # unlimited meters used a hardcoded #111111 — must track the theme instead
        h = s.bars_html({"used_bytes": 0, "limit_bytes": 0, "expiry_ts": 0, "created_ts": 0})
        self.assertIn("var(--ink)", h)
        self.assertNotIn("#111111", h)


if __name__ == "__main__":
    unittest.main()
