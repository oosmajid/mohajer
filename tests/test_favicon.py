import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class FaviconTests(unittest.TestCase):
    def test_page_has_yellow_square_favicon(self):
        h = bot._page("t", "<div>x</div>")
        self.assertIn('rel=icon', h)
        self.assertIn('data:image/svg+xml', h)
        # brutalism yellow fill + ink border; '#' must survive %-formatting as %23 (not %%23)
        self.assertIn('%23FFDD2D', h)
        self.assertIn('%23111', h)
        self.assertNotIn('%%23', h)   # regression guard: the %-escape leaked


if __name__ == "__main__":
    unittest.main()
