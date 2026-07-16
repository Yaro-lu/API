import unittest
from unittest import mock

from app.engines.comfyui_client import ComfyUIClient


class ComfyUIClientTimeoutTests(unittest.TestCase):
    def test_history_requests_have_a_finite_timeout(self):
        response = mock.Mock()
        response.json.return_value = {}
        with mock.patch(
            "app.engines.comfyui_client.requests.get", return_value=response
        ) as get:
            ComfyUIClient().get_history("prompt-test")

        get.assert_called_once_with(
            "http://127.0.0.1:8188/history/prompt-test", timeout=30
        )

    def test_queue_requests_have_a_finite_timeout(self):
        response = mock.Mock()
        response.json.return_value = {}
        with mock.patch(
            "app.engines.comfyui_client.requests.get", return_value=response
        ) as get:
            ComfyUIClient().get_queue_status()

        get.assert_called_once_with("http://127.0.0.1:8188/queue", timeout=30)


if __name__ == "__main__":
    unittest.main()
