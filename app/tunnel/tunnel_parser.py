"""
解析 cloudflared 输出中的 trycloudflare URL
"""
import re
from typing import Optional

# 匹配 https://xxxx.trycloudflare.com
_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def parse_url(line: str) -> Optional[str]:
    """从一行输出中提取 trycloudflare URL"""
    m = _URL_PATTERN.search(line)
    return m.group(0) if m else None


def parse_url_from_text(text: str) -> Optional[str]:
    """从多行文本中提取 trycloudflare URL"""
    for line in text.splitlines():
        url = parse_url(line)
        if url:
            return url
    return None
