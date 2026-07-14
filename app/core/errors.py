"""
API 错误码定义
"""


class APIError(Exception):
    """通用 API 错误"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def unauthorized(message: str = "Invalid API key"):
    return APIError("unauthorized", message, 401)


def invalid_request(message: str):
    return APIError("invalid_request", message, 400)


def queue_full():
    return APIError("queue_full", "Task queue is full", 429)


def missing_prompt():
    return APIError("missing_prompt", "Missing prompt", 400)


def missing_first_frame():
    return APIError("missing_first_frame", "Missing first_frame_url", 400)


def missing_last_frame():
    return APIError("missing_last_frame", "Missing last_frame_url", 400)


def input_download_failed(message: str = "Failed to download input"):
    return APIError("input_download_failed", message, 502)


def unsupported_model(model: str):
    return APIError("unsupported_model", f"Model '{model}' not found", 404)


def unsupported_workflow(workflow_id: str):
    return APIError("unsupported_workflow", f"Workflow '{workflow_id}' not found", 404)


def comfyui_not_ready():
    return APIError(
        "comfyui_not_ready", "ComfyUI is not running or not reachable", 503
    )


def generation_failed(message: str = "Generation failed"):
    return APIError("generation_failed", message, 500)


def file_not_found(filename: str = ""):
    return APIError("file_not_found", f"File not found: {filename}", 404)


def tunnel_not_ready():
    return APIError("tunnel_not_ready", "Tunnel is not ready", 503)
