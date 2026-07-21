"""
Cloudflare Quick Tunnel 管理器
"""
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from app.tunnel.tunnel_parser import parse_url
from app.tunnel.tunnel_state import TunnelState


class CloudflaredManager:
    _MAX_AUTO_RESTARTS = 5
    _STABLE_ONLINE_SECONDS = 60

    def __init__(
        self,
        cloudflared_path: Path,
        local_url: str = "http://127.0.0.1:18188",
        protocol: str = "auto",
    ):
        self.cloudflared_path = cloudflared_path
        self.local_url = local_url
        self.protocol = protocol  # "auto" | "http2"
        self.state = TunnelState()
        self.state.protocol = protocol
        self._process: Optional[subprocess.Popen] = None
        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self._desired_running = False
        self._watch_thread: Optional[threading.Thread] = None
        self._on_url_callback: Optional[Callable[[str], None]] = None
        self._auto_restart_count = 0
        self._online_since: Optional[float] = None
        self._failure_generation: Optional[int] = None

    def set_on_url(self, callback: Callable[[str], None]):
        self._on_url_callback = callback

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._is_live_current_locked():
                return True

            self._desired_running = True
            self._auto_restart_count = 0
            self._online_since = None
            if not self._invalidate_current_locked():
                return False
            return self._spawn_locked(auto_restart=False)

    def stop(self):
        with self._lifecycle_lock:
            self._desired_running = False
            self._auto_restart_count = 0
            self._online_since = None
            if self._invalidate_current_locked():
                self.state.status = "offline"
                self.state.base_url = ""
                self.state.error = ""

    def restart(self) -> bool:
        # Keep stop/replace/start in one critical section. A concurrent stop waits
        # and then invalidates the newly started generation, so stop always wins.
        with self._lifecycle_lock:
            self._desired_running = True
            self._auto_restart_count = 0
            self._online_since = None
            if not self._invalidate_current_locked():
                return False
            return self._spawn_locked(auto_restart=False)

    def _spawn_locked(self, *, auto_restart: bool) -> bool:
        if not self._desired_running:
            return False
        if not self.cloudflared_path.exists():
            self._desired_running = False
            self._set_failed_locked(
                f"cloudflared not found: {self.cloudflared_path}"
            )
            return False

        cmd = [str(self.cloudflared_path), "tunnel"]
        if self.protocol == "http2":
            cmd.extend(["--protocol", "http2"])
        cmd.extend(["--url", self.local_url])

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self._desired_running = False
            self._set_failed_locked(str(exc))
            return False

        self._generation += 1
        generation = self._generation
        self._process = process
        self._online_since = None
        self._failure_generation = None
        if auto_restart:
            self._set_retrying_locked()
        else:
            self.state.status = "starting"
            self.state.base_url = ""
            self.state.error = ""

        try:
            watcher = threading.Thread(
                target=self._watch_output,
                args=(process, generation),
                daemon=True,
            )
            self._watch_thread = watcher
            watcher.start()
        except Exception as exc:
            self._desired_running = False
            if self._invalidate_current_locked():
                self._set_failed_locked(str(exc))
            return False
        return True

    def _invalidate_current_locked(self) -> bool:
        self._generation += 1
        process = self._process
        self._watch_thread = None
        if process is None:
            return True
        if self._terminate_process_locked(process):
            self._process = None
            return True

        # Keep ownership of a process that could not be stopped. Losing this
        # handle and spawning a replacement would recreate the duplicate tunnel.
        self._process = process
        self._desired_running = False
        self._set_failed_locked(
            "Failed to stop cloudflared; refusing to start a replacement"
        )
        return False

    @staticmethod
    def _terminate_process_locked(process) -> bool:
        try:
            if process.poll() is not None:
                return True
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=10)
            return True
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
                process.wait(timeout=3)
                return True
            except (subprocess.TimeoutExpired, OSError):
                try:
                    return process.poll() is not None
                except OSError:
                    return False

    def _owns_generation_locked(self, process, generation: int) -> bool:
        return (
            self._desired_running
            and self._process is process
            and self._generation == generation
        )

    def _is_live_current_locked(
        self,
        process=None,
        generation: Optional[int] = None,
    ) -> bool:
        current = self._process if process is None else process
        if current is None:
            return False
        if process is not None and generation is not None:
            if not self._owns_generation_locked(process, generation):
                return False
        elif not self._desired_running or self._process is not current:
            return False
        return current.poll() is None

    def _verify_public_url(self, url: str, process, generation: int) -> bool:
        health_url = f"{url.rstrip('/')}/health"
        for attempt in range(12):
            with self._lifecycle_lock:
                if not self._is_live_current_locked(process, generation):
                    return False
            try:
                req = urllib.request.Request(
                    health_url,
                    headers={"User-Agent": "lingjing-local-client-tunnel-check/1.0"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    reachable = 200 <= resp.status < 500
            except (OSError, urllib.error.URLError, urllib.error.HTTPError):
                reachable = False

            if reachable:
                with self._lifecycle_lock:
                    return self._is_live_current_locked(process, generation)
            if attempt < 11:
                time.sleep(2)
        return False

    def _accept_or_retry_url(self, url: str, process, generation: int) -> bool:
        exit_error = ""
        with self._lifecycle_lock:
            if not self._owns_generation_locked(process, generation):
                return False
            if self._failure_generation == generation:
                return False
            if process.poll() is not None:
                exit_error = "cloudflared exited while publishing its URL"
            else:
                self._set_retrying_locked()

        if exit_error:
            self._handle_automatic_failure(
                process,
                generation,
                exit_error,
                delay=2,
            )
            return False

        if self._verify_public_url(url, process, generation):
            exit_error = ""
            with self._lifecycle_lock:
                if not self._owns_generation_locked(process, generation):
                    return False
                if self._failure_generation == generation:
                    return False
                if process.poll() is not None:
                    exit_error = "cloudflared exited after URL verification"
                else:
                    self._online_since = time.monotonic()
                    self.state.set_online(url)
                    if self._on_url_callback:
                        try:
                            self._on_url_callback(url)
                        except Exception as exc:
                            print(
                                f"[Tunnel] URL callback failed: {exc}",
                                file=sys.stderr,
                            )
                    return True

            self._handle_automatic_failure(
                process,
                generation,
                exit_error,
                delay=2,
            )
            return False

        self._handle_automatic_failure(
            process,
            generation,
            f"Tunnel URL is not reachable from public network: {url}",
            delay=2,
        )
        return False

    def _launch_url_verification(self, url: str, process, generation: int) -> bool:
        with self._lifecycle_lock:
            if not self._owns_generation_locked(process, generation):
                return False
            try:
                verifier = threading.Thread(
                    target=self._accept_or_retry_url,
                    args=(url, process, generation),
                    daemon=True,
                )
                verifier.start()
                return True
            except Exception as exc:
                launch_error = f"Failed to start tunnel URL verifier: {exc}"

        self._handle_automatic_failure(
            process,
            generation,
            launch_error,
            delay=0,
        )
        return False

    def _handle_automatic_failure(
        self,
        process,
        generation: int,
        error: str,
        *,
        delay: float,
    ) -> bool:
        with self._lifecycle_lock:
            if not self._owns_generation_locked(process, generation):
                return False
            if self._failure_generation == generation:
                return False
            self._failure_generation = generation

            if (
                self._online_since is not None
                and time.monotonic() - self._online_since
                >= self._STABLE_ONLINE_SECONDS
            ):
                self._auto_restart_count = 0
            self._online_since = None
            self._auto_restart_count += 1
            if self._auto_restart_count > self._MAX_AUTO_RESTARTS:
                self._desired_running = False
                if self._invalidate_current_locked():
                    self._set_failed_locked(error)
                return False

            self._set_retrying_locked(error)

        return self._restart_current_after_delay(
            process,
            generation,
            delay=delay,
        )

    def _restart_current_after_delay(
        self,
        process,
        generation: int,
        *,
        delay: float,
    ) -> bool:
        if delay > 0:
            time.sleep(delay)
        with self._lifecycle_lock:
            if not self._owns_generation_locked(process, generation):
                return False
            if not self._invalidate_current_locked():
                return False
            return self._spawn_locked(auto_restart=True)

    def _set_retrying_locked(self, error: str = ""):
        self.state.set_retrying()
        self.state.base_url = ""
        self.state.error = error

    def _set_failed_locked(self, error: str):
        self.state.set_failed(error)
        self.state.base_url = ""

    def _watch_output(self, process, generation: int):
        found_url = False
        stdout = process.stdout
        if stdout is None:
            self._handle_automatic_failure(
                process,
                generation,
                "cloudflared output pipe is unavailable",
                delay=3,
            )
            return

        while True:
            with self._lifecycle_lock:
                if not self._owns_generation_locked(process, generation):
                    return
            try:
                line = stdout.readline()
            except (OSError, ValueError) as exc:
                self._handle_automatic_failure(
                    process,
                    generation,
                    f"Failed to read cloudflared output: {exc}",
                    delay=3,
                )
                return

            if line:
                url = parse_url(line.strip())
                if url and not found_url:
                    found_url = True
                    if not self._launch_url_verification(
                        url,
                        process,
                        generation,
                    ):
                        return
                continue

            return_code = process.poll()
            if return_code is None:
                time.sleep(0.1)
                continue

            reason = (
                "cloudflared exited unexpectedly after publishing a URL"
                if found_url
                else "cloudflared exited before publishing a URL"
            )
            self._handle_automatic_failure(
                process,
                generation,
                f"{reason} (exit code {return_code})",
                delay=3,
            )
            return

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._is_live_current_locked()

    @property
    def is_online(self) -> bool:
        with self._lifecycle_lock:
            return (
                self._is_live_current_locked()
                and self.state.status == "online"
                and bool(self.state.base_url)
            )
