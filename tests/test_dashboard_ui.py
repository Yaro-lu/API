import threading
import unittest
from unittest import mock

from app.gui.main_gateway import GatewayApp


PAGE_IDS = (
    "overview",
    "services",
    "workflows",
    "tasks",
    "network",
    "resources",
    "settings",
)


class DashboardShellTests(unittest.TestCase):
    def test_dashboard_shell_builds_and_switches_all_pages(self):
        """The static shell must not require backend threads to render."""
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update()

                self.assertEqual(tuple(app._pages), PAGE_IDS)
                self.assertEqual(tuple(app._nav_buttons), PAGE_IDS)

                for page_id in PAGE_IDS:
                    app._show_page(page_id)
                    app.update_idletasks()
                    self.assertEqual(app._current_page_id, page_id)
                    self.assertTrue(app._pages[page_id].winfo_ismapped())

                app.geometry("1090x700")
                app.update_idletasks()
                self.assertGreaterEqual(app._page_host.winfo_width(), 870)
                self.assertGreaterEqual(app._page_host.winfo_height(), 560)
                for page in app._pages.values():
                    self.assertLessEqual(page.winfo_reqwidth(), app._page_host.winfo_width())
                    self.assertLessEqual(page.winfo_reqheight(), app._page_host.winfo_height())
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main()
