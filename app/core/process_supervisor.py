"""Own and reliably stop background process trees started by the desktop app."""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil


if os.name == "nt":
    from ctypes import wintypes

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class WindowsProcessJob:
    """A per-service Windows Job Object that kills descendants on close."""

    def __init__(self, name: str):
        self.name = name
        self.handle = None
        self.error = ""
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, f"LingjingGateway-{name}-{os.getpid()}-{id(self)}")
        if not handle:
            self.error = f"CreateJobObject failed: {ctypes.get_last_error()}"
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.error = f"SetInformationJobObject failed: {ctypes.get_last_error()}"
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, process) -> bool:
        if os.name != "nt" or not self.handle:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        process_handle = getattr(process, "_handle", None)
        opened_handle = None
        if process_handle is None:
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            opened_handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(process.pid))
            process_handle = opened_handle
        try:
            if not process_handle or not kernel32.AssignProcessToJobObject(self.handle, process_handle):
                self.error = f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"
                return False
            return True
        finally:
            if opened_handle:
                kernel32.CloseHandle(opened_handle)

    def close(self):
        if os.name != "nt" or not self.handle:
            return
        handle, self.handle = self.handle, None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


@dataclass
class OwnedProcess:
    role: str
    process: object
    pid: int
    create_time: float
    job: Optional[WindowsProcessJob]


class ProcessSupervisor:
    """Track only processes started by this client and clean their trees."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()
        # A role may temporarily have more than one record while a failed
        # restart is being cleaned up.  Never overwrite the old owner: doing so
        # would make a stuck process impossible to reap on final exit.
        self._owned: dict[str, list[OwnedProcess]] = {}
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._closing = False

    def launch(self, role: str, args, **popen_kwargs):
        """Start a process inside its Job before any child can be created.

        On Windows CREATE_SUSPENDED closes the Popen -> AssignJob race.  The
        process is resumed only after it is registered, so every descendant
        automatically joins the same kill-on-close Job.
        """
        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("客户端正在退出，已拒绝启动新的后台进程")
            if os.name == "nt":
                flags = int(popen_kwargs.pop("creationflags", 0))
                popen_kwargs["creationflags"] = flags | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            process = subprocess.Popen(args, **popen_kwargs)
            record = None
            try:
                record = self.register(role, process)
                if os.name == "nt":
                    psutil.Process(process.pid).resume()
                return process
            except BaseException:
                if os.name == "nt":
                    try:
                        psutil.Process(process.pid).resume()
                    except psutil.Error:
                        pass
                if record is not None:
                    self._remove_record(record)
                    self._terminate_record(record, timeout=2.0)
                else:
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except Exception:
                        pass
                raise

    def _reject_unmanaged_process(self, role: str, process, pid: int, create_time: float):
        rejected = OwnedProcess(role, process, pid, create_time, None)
        cleanup_error = self._terminate_without_job(rejected, timeout=2.0)
        detail = f"；清理失败：{cleanup_error}" if cleanup_error else ""
        return detail

    def run(self, role: str, args, *, timeout=None, check=False, capture_output=False, input=None, **popen_kwargs):
        """A subprocess.run-compatible helper whose process is exit-managed."""
        if input is not None and popen_kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        if input is not None:
            popen_kwargs["stdin"] = subprocess.PIPE
        if capture_output:
            if popen_kwargs.get("stdout") is not None or popen_kwargs.get("stderr") is not None:
                raise ValueError("stdout/stderr and capture_output may not both be used")
            popen_kwargs["stdout"] = subprocess.PIPE
            popen_kwargs["stderr"] = subprocess.PIPE

        process = self.launch(role, args, **popen_kwargs)
        record = self._record_for_process(process)
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            if record is not None:
                self._terminate_one(record, timeout=3.0)
            raise
        finally:
            if record is not None and process.poll() is not None:
                self._release_completed(record)

        completed = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            completed.check_returncode()
        return completed

    def register(self, role: str, process) -> OwnedProcess:
        """Register a freshly-created Popen before it can be considered ready."""
        with self._lifecycle_lock:
            role = str(role or "service")
            pid = int(process.pid)
            try:
                create_time = psutil.Process(pid).create_time()
            except (psutil.Error, OSError):
                create_time = time.time()
            if self._closing:
                detail = self._reject_unmanaged_process(role, process, pid, create_time)
                raise RuntimeError(f"客户端正在退出，已清理未登记进程{detail}")

            job = WindowsProcessJob(role) if os.name == "nt" else None
            if job and not job.assign(process):
                # A Windows service without a Job cannot meet the product's
                # no-residual-process guarantee.  launch() starts suspended, so
                # rejecting it here is safe and prevents descendants from escaping.
                job_error = job.error or "AssignProcessToJobObject failed"
                job.close()
                detail = self._reject_unmanaged_process(role, process, pid, create_time)
                raise RuntimeError(f"无法建立后台进程组：{job_error}{detail}")
            elif job:
                # Direct callers may register an already-running process.  Assign
                # any children that pre-date the root assignment; future children
                # inherit the Job automatically.  Production launch() avoids this
                # race altogether by starting suspended.
                try:
                    descendants = psutil.Process(pid).children(recursive=True)
                except psutil.Error:
                    descendants = []
                for child in descendants:
                    job.assign(child)

            record = OwnedProcess(role, process, pid, create_time, job)
            with self._lock:
                previous = list(self._owned.get(role, ()))
                self._owned.setdefault(role, []).append(record)
            for prior in previous:
                error = self._terminate_record(prior, timeout=2.0)
                if not error or not self._same_process(prior):
                    self._remove_record(prior)
                else:
                    prior.job = None
            return record

    def is_running(self, role: str) -> bool:
        with self._lock:
            records = list(self._owned.get(role, ()))
        return any(self._same_process(record) for record in records)

    def pid(self, role: str) -> int:
        with self._lock:
            records = list(self._owned.get(role, ()))
        for record in reversed(records):
            if self._same_process(record):
                return record.pid
        return 0

    def remaining(self) -> dict[str, list[int]]:
        """Return the still-running owned PIDs, grouped by role."""
        with self._lock:
            snapshot = {role: list(records) for role, records in self._owned.items()}
        result = {}
        for role, records in snapshot.items():
            pids = [record.pid for record in records if self._same_process(record)]
            if pids:
                result[role] = pids
        return result

    def terminate(self, role: str, timeout: float = 8.0) -> str:
        with self._lifecycle_lock:
            with self._lock:
                records = self._owned.pop(role, [])
            if not records:
                return ""
            errors = []
            survivors = []
            for record in records:
                error = self._terminate_record(record, timeout=timeout)
                still_running = self._same_process(record)
                if error and still_running:
                    errors.append(f"PID {record.pid}: {error}")
                if error and still_running:
                    record.job = None
                    survivors.append(record)
            if survivors:
                with self._lock:
                    self._owned.setdefault(role, [])[:0] = survivors
            return "；".join(errors)

    def shutdown_all(self, timeout: float = 8.0, final: bool = True) -> dict[str, str]:
        """Idempotently terminate every registered service tree."""
        with self._lifecycle_lock:
            if final:
                self._closing = True
            with self._lock:
                roles = list(self._owned)
            results = {}
            # ComfyUI first prevents a task from continuing while its API is being
            # removed; API next also closes its Tunnel descendant.
            ordered = [role for role in ("comfyui", "api") if role in roles]
            ordered.extend(role for role in roles if role not in ordered)
            for role in ordered:
                results[role] = self.terminate(role, timeout=timeout)
            return results

    def prepare_port(self, port: int) -> tuple[bool, str]:
        """Clear only a listener owned by this live supervisor instance."""
        listeners = self._listener_pids(port)
        if not listeners:
            return True, ""
        external = []
        for pid in listeners:
            if pid == os.getpid():
                continue
            record = self._record_for_pid(pid)
            if record is None:
                external.append(pid)
                continue
            error = self._terminate_one(record, timeout=5.0)
            if error:
                return False, error
        if external:
            return False, f"端口 {port} 已被其他程序占用 (PID {', '.join(map(str, external))})"
        deadline = time.time() + 5
        while time.time() < deadline:
            if not self._listener_pids(port):
                return True, ""
            time.sleep(0.1)
        return False, f"端口 {port} 未能释放"

    def _same_process(self, record: OwnedProcess) -> bool:
        try:
            return record.process.poll() is None
        except (AttributeError, OSError):
            return False

    def _record_for_pid(self, pid: int) -> Optional[OwnedProcess]:
        with self._lock:
            records = [record for group in self._owned.values() for record in group]
        for record in records:
            if record.pid == int(pid) and self._same_process(record):
                return record
        return None

    def _record_for_process(self, process) -> Optional[OwnedProcess]:
        with self._lock:
            records = [record for group in self._owned.values() for record in group]
        return next((record for record in records if record.process is process), None)

    def _remove_record(self, record: OwnedProcess) -> None:
        with self._lock:
            records = self._owned.get(record.role, [])
            self._owned[record.role] = [item for item in records if item is not record]
            if not self._owned[record.role]:
                self._owned.pop(record.role, None)

    def _release_completed(self, record: OwnedProcess) -> None:
        # Closing the Job also removes any helper a short-lived command tried
        # to leave behind.
        if record.job:
            record.job.close()
            record.job = None
        self._reap_popen(record.process)
        self._remove_record(record)

    def _terminate_one(self, record: OwnedProcess, timeout: float) -> str:
        error = self._terminate_record(record, timeout)
        if not error or not self._same_process(record):
            self._remove_record(record)
        else:
            record.job = None
        return error

    def _terminate_record(self, record: OwnedProcess, timeout: float) -> str:
        if not self._same_process(record):
            if record.job:
                record.job.close()
            try:
                record.process.wait(timeout=0.2)
            except Exception:
                pass
            return ""

        # Closing a per-service Job kills the root plus every descendant and is
        # the crash-safe path on Windows.
        if record.job:
            record.job.close()
            record.job = None
            try:
                record.process.wait(timeout=max(0.5, timeout))
            except subprocess.TimeoutExpired:
                try:
                    # Popen.terminate/kill use the stable process handle rather
                    # than looking the PID up again.
                    record.process.kill()
                    record.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return str(exc) or f"PID {record.pid} 仍在运行"
            self._reap_popen(record.process)
            return ""

        return self._terminate_without_job(record, timeout)

    @staticmethod
    def _reap_popen(process):
        try:
            process.wait(timeout=0.5)
        except Exception:
            pass

    def _terminate_without_job(self, record: OwnedProcess, timeout: float) -> str:
        """Fallback using already-owned process objects, never a raw PID kill."""
        try:
            root = psutil.Process(record.pid)
            children = root.children(recursive=True)
            child_identities = [(child, child.create_time()) for child in children]
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            children = []
            child_identities = []
        except (psutil.Error, OSError):
            children = []
            child_identities = []

        try:
            if record.process.poll() is None:
                record.process.terminate()
        except OSError as exc:
            return str(exc)

        for child, create_time in reversed(child_identities):
            try:
                if child.create_time() == create_time and child.is_running():
                    child.terminate()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            except (psutil.Error, OSError):
                pass

        try:
            record.process.wait(timeout=max(0.5, timeout / 2))
        except subprocess.TimeoutExpired:
            try:
                record.process.kill()
                record.process.wait(timeout=max(0.5, timeout / 2))
            except (OSError, subprocess.TimeoutExpired) as exc:
                return str(exc) or f"PID {record.pid} 仍在运行"

        _gone, alive = psutil.wait_procs(children, timeout=max(0.5, timeout / 2)) if children else ([], [])
        for child in alive:
            identity = next((created for item, created in child_identities if item is child), None)
            try:
                if identity is not None and child.create_time() == identity:
                    child.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            except (psutil.Error, OSError):
                pass
        if alive:
            _gone, alive = psutil.wait_procs(alive, timeout=1)
        self._reap_popen(record.process)
        return "" if not alive else f"仍有 {len(alive)} 个子进程未退出"

    def _listener_pids(self, port: int) -> list[int]:
        pids = set()
        try:
            for connection in psutil.net_connections(kind="tcp"):
                address = connection.laddr
                if not address or getattr(address, "port", None) != int(port):
                    continue
                if connection.status != psutil.CONN_LISTEN or not connection.pid:
                    continue
                pids.add(int(connection.pid))
        except (psutil.Error, OSError):
            return []
        return sorted(pids)
