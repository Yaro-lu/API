"""
Tunnel 状态跟踪
"""
import time


class TunnelState:
    PROVIDER_CLOUDFLARE_QUICK = "cloudflare_quick_tunnel"

    def __init__(self):
        self.provider = self.PROVIDER_CLOUDFLARE_QUICK
        self.status = "starting"  # starting | online | failed | retrying
        self.base_url = ""
        self.error = ""
        self.started_at = time.time()
        self.protocol = "auto"  # auto | http2

    def set_online(self, base_url: str):
        self.status = "online"
        self.base_url = base_url
        self.error = ""

    def set_failed(self, error: str):
        self.status = "failed"
        self.error = error

    def set_retrying(self):
        self.status = "retrying"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "url": self.base_url,
            "error": self.error,
            "protocol": self.protocol,
            "uptime_seconds": int(time.time() - self.started_at),
        }
