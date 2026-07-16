import threading
import tkinter as tk
import unittest
from unittest import mock

from app.gui.main_gateway import C, GatewayApp


PAGE_IDS = (
    "overview",
    "workflows",
    "resources",
    "settings",
)


class DashboardShellTests(unittest.TestCase):
    @staticmethod
    def _find_by_text(root, text):
        stack = [root]
        while stack:
            widget = stack.pop()
            try:
                if str(widget.cget("text")) == text:
                    return widget
            except Exception:
                pass
            try:
                stack.extend(widget.winfo_children())
            except Exception:
                pass
        return None

    @staticmethod
    def _all_text(root):
        values = []
        stack = [root]
        while stack:
            widget = stack.pop()
            try:
                value = str(widget.cget("text"))
                if value:
                    values.append(value)
            except Exception:
                pass
            try:
                stack.extend(widget.winfo_children())
            except Exception:
                pass
        return values

    @staticmethod
    def _walk_widgets(root):
        yield root
        try:
            children = root.winfo_children()
        except Exception:
            children = []
        for child in children:
            yield from DashboardShellTests._walk_widgets(child)

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
                self.assertEqual(
                    tuple(app._light_groups),
                    ("server", "env", "comfyui", "tunnel", "api"),
                    "The approved runtime-status card must keep its component order",
                )

                for page_id in PAGE_IDS:
                    app._show_page(page_id)
                    app.update_idletasks()
                    self.assertEqual(app._current_page_id, page_id)
                    self.assertTrue(app._pages[page_id].winfo_ismapped())

                app.geometry("1090x700")
                app.update()
                self.assertGreaterEqual(app._page_host.winfo_width(), 870)
                self.assertGreaterEqual(app._page_host.winfo_height(), 560)
                for page in app._pages.values():
                    self.assertLessEqual(page.winfo_reqwidth(), app._page_host.winfo_width())
                    self.assertLessEqual(page.winfo_reqheight(), app._page_host.winfo_height())

                app._show_page("resources")
                app.update()
                open_buttons = []
                for widget in self._walk_widgets(app._pages["resources"]):
                    try:
                        if str(widget.cget("text")) == "打开" and callable(getattr(widget, "invoke", None)):
                            open_buttons.append(widget)
                    except Exception:
                        pass
                self.assertEqual(len(open_buttons), 3)
                self.assertTrue(all(button.winfo_width() > 1 for button in open_buttons))
                page_left = app._pages["resources"].winfo_rootx()
                page_right = page_left + app._pages["resources"].winfo_width()
                self.assertTrue(
                    all(
                        page_left <= button.winfo_rootx()
                        and button.winfo_rootx() + button.winfo_width() <= page_right
                        for button in open_buttons
                    )
                )
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_console_runtime_action_opens_environment_maintenance(self):
        """The approved console card deep-links without starting an install."""
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update()
                install = self._find_by_text(app._overview_page, "安装运行环境")
                self.assertIsNotNone(install)
                app._install_runtime_from_mirror = mock.Mock()

                install.invoke()
                app.update_idletasks()

                self.assertEqual(app._current_page_id, "resources")
                self.assertTrue(app._pages["resources"].winfo_ismapped())
                app._install_runtime_from_mirror.assert_not_called()
                card = app._dashboard_pages._resource_targets.get("runtime")
                self.assertIsNotNone(card)
                if hasattr(card, "_outline"):
                    self.assertEqual(card._outline, C["primary"])
                else:
                    self.assertEqual(card.cget("border_color"), C["primary"])
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_resources_page_exposes_environment_and_model_maintenance(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app._show_page("resources")
                app.update_idletasks()
                texts = set(self._all_text(app._pages["resources"]))

                self.assertIn("运行环境维护", texts)
                self.assertTrue({"检查环境", "一键安装"} & texts)
                self.assertTrue({"修复 / 更新", "本地安装包"} & texts)
                self.assertIn("模型维护", texts)
                self.assertIn("导入已有模型", texts)
                self.assertIn("重新检查", texts)
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_missing_environment_recheck_reports_without_name_error(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app.after = lambda _delay, callback: callback()
        app._finish_runtime_recheck = mock.Mock()
        with mock.patch(
            "app.gui.main_gateway.missing_runtime_paths",
            return_value=["runtime/python/python.exe"],
        ):
            app._retry_env_check()

        result = app._finish_runtime_recheck.call_args.args[0]
        self.assertFalse(result["package_ready"])
        self.assertIn("缺少 1 项", result["message"])

    def test_login_validation_reports_inside_popup_callback(self):
        app = object.__new__(GatewayApp)
        app._server_sync_running = False
        app._get_server_url = lambda: ""
        app._get_server_email = lambda: ""
        app._get_server_password = lambda: ""
        app._set_account_status = mock.Mock()
        results = []

        app._login_and_sync(on_result=lambda success, message: results.append((success, message)))

        self.assertEqual(results, [(False, "请填写服务端地址。")])
        app._set_account_status.assert_called_once_with("请填写服务端地址", "error")

    def test_local_mode_does_not_start_platform_session_refresh(self):
        app = object.__new__(GatewayApp)
        app._initial_session_sync_done = False
        app._server_mode = "guest"
        app._server_session_token = ""
        app._tunnel_url = "https://example.test"
        app._api_key = "sk-local-test"
        with mock.patch.object(threading, "Thread") as thread:
            app._refresh_saved_login_and_sync()
        thread.assert_not_called()

    def test_many_long_named_workflows_keep_every_management_action_reachable(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.geometry("1090x700")
                app.update()
                app._last_health = {
                    "workflows": [
                        {
                            "id": f"workflow_{index}",
                            "name": f"这是一个用于验证窄窗口布局不会裁切操作按钮的超长工作流名称_{index}",
                            "enabled": True,
                            "available": True,
                            "workflow_json": f"workflow_{index}/workflow.json",
                            "output_type": "image",
                            "dependency_status": "ready",
                            "dependencies": {"nodes": ["SaveImage"]},
                            "nodes_verified": True,
                            "is_default": index == 0,
                        }
                        for index in range(6)
                    ]
                }
                app._dashboard_pages._last_snapshot = ""
                app._dashboard_pages.refresh(app._last_health)
                app._show_page("workflows")
                app.update()

                page = app._pages["workflows"]
                buttons = []
                scroll_canvases = []
                for widget in self._walk_widgets(page):
                    try:
                        text = str(widget.cget("text"))
                    except Exception:
                        text = ""
                    if text in {"查看参数", "停用", "设为默认"} and callable(
                        getattr(widget, "invoke", None)
                    ):
                        buttons.append(widget)
                    if isinstance(widget, tk.Canvas):
                        try:
                            if str(widget.cget("yscrollcommand")):
                                scroll_canvases.append(widget)
                        except Exception:
                            pass

                self.assertEqual(sum(str(item.cget("text")) == "查看参数" for item in buttons), 6)
                self.assertEqual(sum(str(item.cget("text")) == "停用" for item in buttons), 6)
                self.assertEqual(sum(str(item.cget("text")) == "设为默认" for item in buttons), 5)
                self.assertTrue(scroll_canvases)
                page_left = page.winfo_rootx()
                page_right = page_left + page.winfo_width()
                self.assertTrue(
                    all(
                        page_left <= button.winfo_rootx()
                        and button.winfo_rootx() + button.winfo_width() <= page_right
                        for button in buttons
                        if button.winfo_ismapped()
                    )
                )
                self.assertLessEqual(page.winfo_reqwidth(), app._page_host.winfo_width())
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_classic_tk_button_converts_pixel_width_to_character_width(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                parent = tk.Frame(app)
                with mock.patch("app.gui.main_gateway.CTK_AVAILABLE", False):
                    button = app._button(parent, "测试", lambda: None, width=112)
                self.assertLessEqual(int(button.cget("width")), 13)
                button.destroy()
                parent.destroy()
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()


if __name__ == "__main__":
    unittest.main()
