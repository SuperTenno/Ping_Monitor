#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器网络连通性实时监测工具 v2.0。

在 v1.2 的基础上重写，主要改进：
  * 图形界面：实时状态表格、单机延迟趋势曲线、事件日志面板；
  * 多线程探测：每个目标独立线程 + 全局并发上限，互不阻塞；
  * 稳定性：子进程超时保护、线程看门狗自动重启、崩溃堆栈落盘、
    日志按日切分并自动清理、配置/列表原子写入、GUI 操作异常不退出；
  * 可维护性：config.json 持久化参数、server_list.txt 热加载、
    支持 IPv4 / IPv6 / 域名、无界面模式（--headless）与单次自检（--once）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import datetime as dt
import ipaddress
import json
import locale
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

try:  # 无图形环境（如 Linux 服务器）只使用 --headless 时允许缺少 tkinter
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox, simpledialog, ttk

    HAS_TK = True
except Exception:  # pragma: no cover - 取决于运行环境
    HAS_TK = False


APP_NAME = "网络连通性监测"
APP_VERSION = "2.0"
WINDOW_TITLE = f"{APP_NAME} v{APP_VERSION}"
REFRESH_MS = 500                 # 界面刷新间隔（毫秒）
MAX_LOG_LINES = 500              # 界面日志面板最多保留的行数
SUMMARY_SECONDS = 10             # 无界面模式摘要输出间隔
GEOMETRY_RE = re.compile(r"^\d{2,4}x\d{2,4}(?:[+-]\d+[+-]\d+)?$")

# ------------------------------- 配色 --------------------------------------
BG = "#eef2f7"
CARD = "#ffffff"
BORDER = "#dbe3ee"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
UP = "#15803d"
DOWN = "#dc2626"
WARN = "#b45309"
HEADER_BG = "#0f172a"
HEADER_CARD = "#17233c"
LOG_BG = "#0b1220"

# ------------------------------- 路径 --------------------------------------
def application_dir() -> Path:
    """返回程序所在目录（兼容 PyInstaller 单文件模式）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = application_dir()


# ============================ 基础工具 ====================================

def safe_print(*values: Any) -> None:
    """在 pythonw / 无控制台环境下也不会抛异常的 print。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(*values)
    except Exception:
        try:
            stream.write(" ".join(str(item) for item in values) + "\n")
        except Exception:
            pass


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clock_text(timestamp: float) -> str:
    if not timestamp:
        return "—"
    return dt.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def datetime_text(timestamp: float) -> str:
    if not timestamp:
        return "—"
    return dt.datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M:%S")


def ago_text(timestamp: float) -> str:
    if not timestamp:
        return "—"
    seconds = max(0.0, time.time() - timestamp)
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{int(seconds // 86400)} 天前"


def clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(high, max(low, number))


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def resolve_path(value: str, base: Path = BASE_DIR) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def usable_geometry(geometry: str, screen_w: int, screen_h: int) -> Optional[str]:
    """只接受完整落在当前屏幕内的窗口位置，避免换显示器后窗口跑到屏幕外。"""
    if not geometry or not GEOMETRY_RE.match(geometry):
        return None
    match = re.match(r"^(\d{2,4})x(\d{2,4})(?:([+-]\d+)([+-]\d+))?$", geometry)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width < 800 or height < 500 or width > screen_w or height > screen_h:
        return None
    if match.group(3):
        x, y = int(match.group(3)), int(match.group(4))
        if x > screen_w - 200 or y > screen_h - 120 or x < 200 - width:
            return None
    return geometry


def atomic_write_text(path: Path, content: str) -> None:
    """先写临时文件再原子替换，避免断电/崩溃留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def append_crash_log(log_dir: Path, text: str) -> None:
    """把未处理异常写入 crash.log，单文件超过 2MB 时轮转一次。"""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "crash.log"
        try:
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                backup = log_dir / "crash.log.1"
                if backup.exists():
                    backup.unlink()
                path.replace(backup)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"\n===== {now_text()} =====\n{text}\n")
    except Exception:
        pass


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def install_exception_hooks(log_dir: Path) -> None:
    """捕获主线程/子线程的未处理异常，保证程序能留下现场。"""

    def hook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        safe_print(text)
        append_crash_log(log_dir, text)

    sys.excepthook = hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)


# ============================== 配置 ======================================

@dataclass
class Config:
    interval: float = 5.0          # 每个目标的探测间隔（秒）
    timeout: float = 2.0           # 单次 ping 超时（秒）
    max_concurrent: int = 16       # 同时进行的 ping 进程上限
    fail_threshold: int = 2        # 连续失败多少次判定为离线
    heartbeat_minutes: int = 0     # 在线状态心跳写日志的间隔（分钟），0 表示关闭
    retain_days: int = 30          # 日志保留天数，0 表示永久保留
    history_size: int = 120        # 延迟趋势保留的采样点数
    log_dir: str = "network_logs"
    server_file: str = "server_list.txt"
    window_geometry: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        config = cls()
        config.interval = clamp_float(data.get("interval"), 1.0, 3600.0, config.interval)
        config.timeout = clamp_float(data.get("timeout"), 0.5, 30.0, config.timeout)
        config.max_concurrent = clamp_int(data.get("max_concurrent"), 1, 64, config.max_concurrent)
        config.fail_threshold = clamp_int(data.get("fail_threshold"), 1, 10, config.fail_threshold)
        config.heartbeat_minutes = clamp_int(data.get("heartbeat_minutes"), 0, 1440, config.heartbeat_minutes)
        config.retain_days = clamp_int(data.get("retain_days"), 0, 3650, config.retain_days)
        config.history_size = clamp_int(data.get("history_size"), 10, 2000, config.history_size)
        for key in ("log_dir", "server_file"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                setattr(config, key, value.strip())
        geometry = data.get("window_geometry")
        if isinstance(geometry, str) and GEOMETRY_RE.match(geometry.strip()):
            config.window_geometry = geometry.strip()
        return config

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interval": self.interval,
            "timeout": self.timeout,
            "max_concurrent": self.max_concurrent,
            "fail_threshold": self.fail_threshold,
            "heartbeat_minutes": self.heartbeat_minutes,
            "retain_days": self.retain_days,
            "history_size": self.history_size,
            "log_dir": self.log_dir,
            "server_file": self.server_file,
            "window_geometry": self.window_geometry,
        }

    def save(self, path: Path) -> None:
        atomic_write_text(path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n")


def load_config(path: Path) -> Tuple[Config, Optional[str]]:
    """读取配置；文件损坏时备份并回退到默认值，返回值附带提示信息。"""
    config = Config()
    warning: Optional[str] = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            backup = path.with_name(path.name + ".bad")
            try:
                path.replace(backup)
            except OSError:
                pass
            warning = f"配置文件解析失败，已使用默认参数（原文件备份为 {backup.name}）：{exc}"
            data = {}
        if isinstance(data, dict):
            config = Config.from_dict(data)
        elif warning is None:
            warning = "配置文件内容不是 JSON 对象，已使用默认参数"
    else:
        try:
            config.save(path)
        except OSError as exc:
            warning = f"无法创建配置文件 {path}：{exc}"
    return config, warning


# ============================ 主机列表 ====================================

HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
    r"(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*$"
)
SERVER_TEMPLATE = """# 监测主机列表
# 每行一个 IP 地址或域名（支持 IPv4 / IPv6 / 主机名）
# 以 # 开头的行为注释，行内 # 之后的内容也会被忽略
#
# 示例：
#   8.8.8.8
#   www.example.com
#   ::1
"""


def normalize_host(value: str) -> Optional[str]:
    """校验并规范化主机地址，非法时返回 None。"""
    host = value.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    if not host or len(host) > 253:
        return None
    try:
        address = ipaddress.ip_address(host)
        return str(address) if address.version == 6 else host
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", host):
        return None  # 形似 IPv4 但不合法
    if ".." in host or not HOSTNAME_RE.match(host):
        return None
    return host.lower()


def unique_hosts(hosts: Sequence[str]) -> List[str]:
    seen = set()
    result = []
    for host in hosts:
        key = host.lower()
        if key not in seen:
            seen.add(key)
            result.append(host)
    return result


def load_servers(path: Path) -> Tuple[List[str], List[str]]:
    """读取 server_list.txt，返回 (有效主机, 无效条目)。文件缺失时自动创建模板。"""
    if not path.exists():
        try:
            atomic_write_text(path, SERVER_TEMPLATE)
        except OSError:
            pass
        return [], []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="gbk", errors="ignore")
        except OSError:
            return [], []
    except OSError:
        return [], []
    valid: List[str] = []
    invalid: List[str] = []
    seen = set()
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        token = stripped.split()[0]
        host = normalize_host(token)
        if host is None:
            invalid.append(token)
            continue
        key = host.lower()
        if key not in seen:
            seen.add(key)
            valid.append(host)
    return valid, invalid


def save_servers(path: Path, hosts: Sequence[str]) -> None:
    lines = [
        "# 监测主机列表（由网络连通性监测程序维护，可手工编辑）",
        "# 每行一个 IP 或域名，# 后为注释",
        "",
    ]
    lines.extend(unique_hosts(hosts))
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")


# ============================== 探测 ======================================

RTT_RE = re.compile(
    r"(?:time|时间|tiempo|temps|zeit|tijd)\s*[=<]\s*(\d+(?:[.,]\d+)?)\s*ms",
    re.IGNORECASE,
)
UNREACHABLE_MARKERS = (
    "100% loss",
    "100% 丢失",
    "100% packet loss",
    "request timed out",
    "请求超时",
    "destination host unreachable",
    "destination net unreachable",
    "无法访问目标主机",
    "目标主机无法访问",
    "transmit failed",
    "传输失败",
    "general failure",
    "一般故障",
    "ttl expired",
    "ttl 传输中过期",
    "could not find host",
    "找不到主机",
    "name or service not known",
)


@dataclass
class ProbeResult:
    host: str
    ok: bool
    rtt: Optional[float]
    checked_at: float
    error: Optional[str] = None


def is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def console_encodings() -> List[str]:
    """猜测控制台程序的输出编码（中文 Windows 下 ping 使用 ANSI 代码页）。"""
    candidates: List[str] = []
    if os.name == "nt":
        try:
            candidates.append(f"cp{ctypes.windll.kernel32.GetACP()}")
            candidates.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")
        except Exception:
            pass
    candidates.extend(["utf-8", locale.getpreferredencoding(False), "latin-1"])
    result: List[str] = []
    seen = set()
    for encoding in candidates:
        if encoding and encoding.lower() not in seen:
            seen.add(encoding.lower())
            result.append(encoding)
    return result


def decode_output(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in console_encodings():
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def build_ping_command(host: str, timeout: float) -> List[str]:
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", str(max(200, int(round(timeout * 1000))))]
        if is_ipv6(host):
            command.insert(1, "-6")
    else:
        wait = str(max(1, int(math.ceil(timeout))))
        if is_ipv6(host):
            executable = "ping6" if shutil.which("ping6") else "ping"
            command = [executable, "-c", "1", "-W", wait]
        else:
            command = ["ping", "-c", "1", "-W", wait]
    command.append(host)
    return command


def parse_rtt(text: str) -> Optional[float]:
    matches = RTT_RE.findall(text)
    if not matches:
        return None
    try:
        return round(float(matches[-1].replace(",", ".")), 1)
    except ValueError:
        return None


def looks_unreachable(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in UNREACHABLE_MARKERS)


def describe_failure(text: str) -> str:
    lowered = text.lower()
    if "could not find host" in lowered or "找不到主机" in text or "name or service not known" in lowered:
        return "域名解析失败"
    if "timed out" in lowered or "请求超时" in text:
        return "请求超时"
    if "unreachable" in lowered or "无法访问" in text or "不可达" in text:
        return "目标不可达"
    if "general failure" in lowered or "一般故障" in text or "transmit failed" in lowered:
        return "网络故障"
    return "无响应"


def ping_once(host: str, timeout: float) -> ProbeResult:
    """执行一次 ping。任何异常都转换成 ProbeResult，绝不向上抛出。"""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            build_ping_command(host, timeout),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 5,
            creationflags=creation_flags(),
        )
        elapsed_ms = (time.monotonic() - started) * 1000
        stdout = decode_output(completed.stdout)
        stderr = decode_output(completed.stderr)
        if completed.returncode == 0 and not looks_unreachable(stdout):
            rtt = parse_rtt(stdout)
            if rtt is None:
                rtt = round(min(elapsed_ms, timeout * 1000), 1)
            return ProbeResult(host, True, rtt, time.time(), None)
        detail = (stdout + "\n" + stderr).strip()
        return ProbeResult(host, False, None, time.time(), describe_failure(detail))
    except subprocess.TimeoutExpired:
        return ProbeResult(host, False, None, time.time(), "探测超时")
    except FileNotFoundError:
        return ProbeResult(host, False, None, time.time(), "找不到 ping 命令")
    except Exception as exc:  # 包括进程创建失败等
        return ProbeResult(host, False, None, time.time(), f"{type(exc).__name__}: {exc}")


# ============================== 日志 ======================================

class EventLogger:
    """线程安全的按日日志写入器，日志格式兼容 v1.2。"""

    def __init__(
        self,
        log_dir: Path,
        retain_days: int = 30,
        echo: bool = False,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.log_dir = log_dir
        self.retain_days = retain_days
        self.echo = echo
        self.on_error = on_error
        self._lock = threading.Lock()
        self._handle: Optional[Any] = None
        self._date: Optional[dt.date] = None
        self._closed = False
        self._last_error_at = 0.0

    # ------------------------------------------------ 对外接口
    def prepare(self) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._notify_error(f"无法创建日志目录 {self.log_dir}：{exc}")
        self._cleanup_old_logs()

    def set_retention(self, days: int) -> None:
        self.retain_days = max(0, int(days))
        self._cleanup_old_logs()

    def write(self, level: str, message: str) -> str:
        line = f"{now_text()} [{level}] {message}"
        self._write_line(line)
        return line

    def log_status(self, host: str, status: str, rtt: Optional[float]) -> str:
        timestamp = now_text()
        if status == "up":
            rtt_text = f"{rtt:g}" if isinstance(rtt, (int, float)) else "?"
            line = f"{timestamp}  {host} is reachable. Response time: {rtt_text}ms"
        else:
            line = f"{timestamp} [DOWN] {host} is unreachable!"
        self._write_line(line)
        return line

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_handle()

    # ------------------------------------------------ 内部实现
    def _cleanup_old_logs(self) -> None:
        if self.retain_days <= 0:
            return
        cutoff = dt.date.today() - dt.timedelta(days=self.retain_days)
        try:
            for path in self.log_dir.glob("network_monitor_*.txt"):
                match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
                if not match:
                    continue
                try:
                    day = dt.date.fromisoformat(match.group(1))
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def _ensure_handle(self) -> None:
        today = dt.date.today()
        if self._handle is not None and self._date == today:
            return
        self._close_handle()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"network_monitor_{today.isoformat()}.txt"
        self._handle = open(path, "a", encoding="utf-8", newline="\n")
        self._date = today

    def _write_line(self, line: str) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._ensure_handle()
                assert self._handle is not None
                self._handle.write(line + "\n")
                self._handle.flush()
            except Exception as exc:
                self._close_handle()
                self._notify_error(f"写入日志失败：{exc}")
        if self.echo:
            safe_print(line)

    def _close_handle(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                self._handle.close()
            except Exception:
                pass
        self._handle = None

    def _notify_error(self, message: str) -> None:
        current = time.time()
        if current - self._last_error_at < 60:
            return
        self._last_error_at = current
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                pass
        else:
            safe_print(f"[日志错误] {message}")


# ============================== 监测引擎 ==================================

@dataclass
class TargetState:
    host: str
    status: str = "unknown"          # unknown / up / down
    last_rtt: Optional[float] = None
    last_error: Optional[str] = None
    samples: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    last_probe_at: float = 0.0
    last_change_at: float = 0.0
    last_ok_at: float = 0.0
    last_heartbeat_at: float = 0.0
    down_since: Optional[float] = None
    history: Deque[Tuple[float, Optional[float]]] = field(default_factory=lambda: deque(maxlen=120))


class MonitorEngine:
    """每个目标一个常驻线程；看门狗负责重启异常退出的线程。"""

    def __init__(
        self,
        hosts: Sequence[str],
        config: Config,
        logger: EventLogger,
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._emit = emit or (lambda event: None)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._running = False
        self._states: Dict[str, TargetState] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._wakes: Dict[str, threading.Event] = {}
        self._restarts: Dict[str, Deque[float]] = {}
        self._restart_block_until: Dict[str, float] = {}
        self._semaphore = threading.Semaphore(max(1, int(config.max_concurrent)))
        self._watchdog: Optional[threading.Thread] = None
        for host in unique_hosts(hosts):
            self._create_state(host)

    # ------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stop.clear()
            self._running = True
            hosts = list(self._states.keys())
        for host in hosts:
            self._start_worker(host)
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="monitor-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            self._running = False
            self._stop.set()
            wakes = list(self._wakes.values())
            threads = [thread for thread in self._threads.values() if thread.is_alive()]
            watchdog = self._watchdog
        for wake in wakes:
            wake.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                thread.join(timeout=remaining)
            except RuntimeError:
                pass
        if watchdog is not None and watchdog.is_alive() and watchdog is not threading.current_thread():
            try:
                watchdog.join(timeout=max(0.0, deadline - time.monotonic()))
            except RuntimeError:
                pass
        self._watchdog = None

    # ------------------------------------------------ 状态
    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def is_running(self) -> bool:
        return self._running

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        with self._lock:
            wakes = list(self._wakes.values())
        for wake in wakes:
            wake.set()

    def set_hosts(self, hosts: Sequence[str]) -> Tuple[List[str], List[str]]:
        wanted = unique_hosts(hosts)
        wanted_set = set(wanted)
        added: List[str] = []
        removed: List[str] = []
        with self._lock:
            for host in list(self._states.keys()):
                if host not in wanted_set:
                    removed.append(host)
                    self._states.pop(host, None)
                    wake = self._wakes.pop(host, None)
                    if wake is not None:
                        wake.set()
                    self._threads.pop(host, None)
                    self._restarts.pop(host, None)
                    self._restart_block_until.pop(host, None)
            for host in wanted:
                if host not in self._states:
                    self._create_state(host)
                    added.append(host)
        for host in added:
            self._start_worker(host)
        return added, removed

    def update_concurrency(self) -> None:
        """并发上限修改后立即生效。"""
        with self._lock:
            self._semaphore = threading.Semaphore(max(1, int(self._config.max_concurrent)))

    def probe_now(self, host: Optional[str] = None) -> None:
        with self._lock:
            targets = [host] if host else list(self._states.keys())
            wakes = [self._wakes.get(target) for target in targets]
        for wake in wakes:
            if wake is not None:
                wake.set()

    def snapshot(self, history_host: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            result: List[Dict[str, Any]] = []
            for host, state in self._states.items():
                total = state.samples
                result.append(
                    {
                        "host": host,
                        "status": state.status,
                        "rtt": state.last_rtt,
                        "samples": total,
                        "successes": state.successes,
                        "availability": (state.successes / total) if total else None,
                        "consecutive_failures": state.consecutive_failures,
                        "last_probe_at": state.last_probe_at,
                        "last_change_at": state.last_change_at,
                        "last_ok_at": state.last_ok_at,
                        "last_error": state.last_error,
                        "down_since": state.down_since,
                        "history_len": len(state.history),
                        "history": list(state.history) if host == history_host else [],
                    }
                )
            return result

    # ------------------------------------------------ 内部实现
    def _create_state(self, host: str) -> None:
        size = max(10, int(self._config.history_size))
        self._states[host] = TargetState(host=host, history=deque(maxlen=size))

    def _is_tracked(self, host: str) -> bool:
        with self._lock:
            return host in self._states

    def _start_worker(self, host: str) -> None:
        with self._lock:
            if not self._running or host not in self._states:
                return
            existing = self._threads.get(host)
            if existing is not None and existing.is_alive():
                return
            wake = self._wakes.setdefault(host, threading.Event())
            thread = threading.Thread(
                target=self._worker_entry,
                args=(host, wake),
                name=f"monitor-{host}",
                daemon=True,
            )
            self._threads[host] = thread
            thread.start()

    def _worker_entry(self, host: str, wake: threading.Event) -> None:
        try:
            self._worker_loop(host, wake)
        except Exception:
            self._logger.write("ERROR", f"{host} 监测线程异常退出：{traceback.format_exc(limit=3).strip()}")
            self._emit({"type": "line", "level": "error", "text": f"{now_text()} [ERROR] {host} 监测线程异常退出，看门狗将尝试恢复"})

    def _worker_loop(self, host: str, wake: threading.Event) -> None:
        rng = random.Random()
        interval = max(1.0, float(self._config.interval))
        next_run = time.monotonic() + rng.uniform(0.0, min(1.0, interval * 0.35))
        while not self._stop.is_set():
            if not self._is_tracked(host):
                return
            if self._paused.is_set():
                self._stop.wait(0.2)
                continue
            current = time.monotonic()
            if current < next_run:
                if wake.wait(min(0.5, next_run - current)):
                    wake.clear()
                    next_run = 0.0
                continue
            interval = max(1.0, float(self._config.interval))
            timeout = max(0.5, float(self._config.timeout))
            with self._semaphore:
                if self._stop.is_set():
                    break
                result = ping_once(host, timeout)
            if not self._is_tracked(host):
                return
            self._apply_result(host, result)
            jitter = rng.uniform(0.0, min(1.0, interval * 0.15))
            next_run = time.monotonic() + max(0.5, interval) + jitter

    def _apply_result(self, host: str, result: ProbeResult) -> None:
        current = time.time()
        action: Optional[str] = None
        with self._lock:
            state = self._states.get(host)
            if state is None:
                return
            state.samples += 1
            state.last_probe_at = current
            if result.ok:
                state.successes += 1
                state.consecutive_failures = 0
                state.last_rtt = result.rtt
                state.last_ok_at = current
                new_status = "up"
            else:
                state.consecutive_failures += 1
                state.last_rtt = None
                state.last_error = result.error or "不可达"
                threshold = max(1, int(self._config.fail_threshold))
                new_status = "down" if state.consecutive_failures >= threshold else state.status
            state.history.append((current, result.rtt if result.ok else None))
            if new_status != state.status:
                state.status = new_status
                state.last_change_at = current
                state.down_since = current if new_status == "down" else None
                if new_status == "up":
                    state.last_heartbeat_at = current
                action = new_status
            elif new_status == "up" and self._heartbeat_due(state, current):
                state.last_heartbeat_at = current
                action = "heartbeat"

        if action == "up":
            line = self._logger.log_status(host, "up", result.rtt)
            self._emit({"type": "line", "level": "up", "text": line})
        elif action == "down":
            line = self._logger.log_status(host, "down", None)
            self._emit({"type": "line", "level": "down", "text": line})
        elif action == "heartbeat":
            line = self._logger.log_status(host, "up", result.rtt)
            self._emit({"type": "line", "level": "info", "text": line})

    def _heartbeat_due(self, state: TargetState, current: float) -> bool:
        minutes = int(self._config.heartbeat_minutes)
        if minutes <= 0:
            return False
        return current - state.last_heartbeat_at >= minutes * 60

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(5.0):
            with self._lock:
                if not self._running:
                    return
                hosts = list(self._states.keys())
                threads = dict(self._threads)
            for host in hosts:
                thread = threads.get(host)
                if thread is not None and thread.is_alive():
                    continue
                if not self._is_tracked(host):
                    continue
                now = time.monotonic()
                blocked = self._restart_block_until.get(host, 0.0)
                if now < blocked:
                    continue
                history = self._restarts.setdefault(host, deque(maxlen=20))
                while history and now - history[0] > 60:
                    history.popleft()
                if len(history) >= 5:
                    self._restart_block_until[host] = now + 60
                    line = self._logger.write("ERROR", f"{host} 监测线程连续异常，暂停 60 秒后重试")
                    self._emit({"type": "line", "level": "error", "text": line})
                    continue
                history.append(now)
                line = self._logger.write("WARN", f"{host} 监测线程已退出，正在自动重启")
                self._emit({"type": "line", "level": "warn", "text": line})
                self._start_worker(host)


# ============================== 图形界面 ==================================

if HAS_TK:

    class MonitorApp:
        """tkinter 主窗口：状态表格 + 延迟趋势 + 事件日志。"""

        def __init__(
            self,
            root: "tk.Tk",
            config: Config,
            config_path: Path,
            server_file: Path,
            hosts: Sequence[str],
            engine: MonitorEngine,
            logger: EventLogger,
            events: "queue.Queue[Dict[str, Any]]",
            config_warning: Optional[str] = None,
        ) -> None:
            self.root = root
            self.config = config
            self.config_path = config_path
            self.server_file = server_file
            self.log_dir = logger.log_dir
            self.hosts = list(hosts)
            self.engine = engine
            self.logger = logger
            self.events = events
            self.config_warning = config_warning

            self._shutdown_done = False
            self._closing = False
            self._after_id: Optional[str] = None
            self._tick_count = 0
            self._snapshot: List[Dict[str, Any]] = []
            self._by_host: Dict[str, Dict[str, Any]] = {}
            self._row_sig: Dict[str, Tuple[Any, ...]] = {}
            self._detail_sig: Optional[Tuple[Any, ...]] = None
            self._current_history: List[Tuple[float, Optional[float]]] = []
            self._settings_win: Optional["tk.Toplevel"] = None
            self._server_mtime: Optional[float] = None
            self._current_title = ""

            self._setup_fonts()
            self._build_ui()
            self._bind_shortcuts()
            self.root.report_callback_exception = self._tk_callback_error

            try:
                self._server_mtime = self.server_file.stat().st_mtime
            except OSError:
                self._server_mtime = None

            self.engine.start()
            if config_warning:
                self._append_log(f"{now_text()} [WARN] {config_warning}", "warn")
            if self.hosts:
                line = self.logger.write(
                    "INFO",
                    f"监测已启动：{len(self.hosts)} 个目标 · 间隔 {config.interval:g}s · 超时 {config.timeout:g}s · 连续失败 {config.fail_threshold} 次判离线",
                )
            else:
                line = self.logger.write("INFO", "监测已启动，但尚未配置任何主机")
            self._append_log(line, "info")
            self._refresh_empty_hint()
            self._tick()

        # ------------------------------------------------ 初始化
        def _setup_fonts(self) -> None:
            available = set(tkfont.families(self.root))
            candidates = ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "DejaVu Sans"]
            self.family = next((name for name in candidates if name in available), "TkDefaultFont")
            if self.family == "TkDefaultFont":
                self.family = tkfont.nametofont("TkDefaultFont").actual("family")
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family=self.family, size=10)
                except Exception:
                    pass
            self.font_title = tkfont.Font(family=self.family, size=17, weight="bold")
            self.font_sub = tkfont.Font(family=self.family, size=9)
            self.font_tiny = tkfont.Font(family=self.family, size=8)
            self.font_card = tkfont.Font(family=self.family, size=20, weight="bold")
            self.font_section = tkfont.Font(family=self.family, size=11, weight="bold")
            self.font_bold = tkfont.Font(family=self.family, size=10, weight="bold")
            self.font_value = tkfont.Font(family=self.family, size=11, weight="bold")
            mono_family = "Consolas" if "Consolas" in available else self.family
            self.font_mono = tkfont.Font(family=mono_family, size=9)

        def _build_ui(self) -> None:
            self.root.title(WINDOW_TITLE)
            self.root.configure(bg=BG)
            self.root.minsize(1000, 700)

            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            width = min(1280, max(1000, screen_w - 120))
            height = min(880, max(700, screen_h - 120))
            saved_geometry = usable_geometry(self.config.window_geometry, screen_w, screen_h)
            if saved_geometry:
                self.root.geometry(saved_geometry)
            else:
                self.root.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 2 - 24)}")

            self._style = ttk.Style(self.root)
            try:
                if "clam" in self._style.theme_names():
                    self._style.theme_use("clam")
            except Exception:
                pass
            self._style.configure(
                "Treeview",
                font=(self.family, 10),
                rowheight=30,
                background=CARD,
                fieldbackground=CARD,
                borderwidth=0,
            )
            self._style.configure(
                "Treeview.Heading",
                font=(self.family, 9, "bold"),
                background="#f1f5f9",
                foreground="#475569",
                relief="flat",
                padding=(6, 8),
            )
            self._style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#1e3a8a")])
            self._style.map("Treeview.Heading", background=[("active", "#e2e8f0")])
            self._style.configure("TPanedwindow", background=BG)

            self._build_menu()
            self._build_header()
            self._build_toolbar()
            self._build_statusbar()
            self._build_panes()
            self._build_log_panel()

        def _build_menu(self) -> None:
            menubar = tk.Menu(self.root)
            file_menu = tk.Menu(menubar, tearoff=0)
            file_menu.add_command(label="重新加载主机列表", command=self._reload_hosts, accelerator="Ctrl+R")
            file_menu.add_command(label="打开日志目录", command=self._open_log_dir)
            file_menu.add_separator()
            file_menu.add_command(label="退出", command=self.on_close, accelerator="Ctrl+Q")
            menubar.add_cascade(label="文件", menu=file_menu)

            monitor_menu = tk.Menu(menubar, tearoff=0)
            monitor_menu.add_command(label="暂停 / 继续监测", command=self._toggle_pause, accelerator="Ctrl+P")
            monitor_menu.add_command(label="立即检测全部", command=self._probe_now, accelerator="F5")
            monitor_menu.add_command(label="立即检测选中", command=self._probe_selected)
            monitor_menu.add_separator()
            monitor_menu.add_command(label="监测设置", command=self._open_settings)
            menubar.add_cascade(label="监测", menu=monitor_menu)

            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="关于", command=self._show_about, accelerator="F1")
            menubar.add_cascade(label="帮助", menu=help_menu)
            self.root.configure(menu=menubar)

        def _build_header(self) -> None:
            header = tk.Frame(self.root, bg=HEADER_BG, height=96)
            header.pack(fill="x")
            header.pack_propagate(False)

            left = tk.Frame(header, bg=HEADER_BG)
            left.pack(side="left", padx=26, pady=16)
            tk.Label(left, text=APP_NAME, bg=HEADER_BG, fg="#f8fafc", font=self.font_title).pack(anchor="w")
            tk.Label(
                left,
                text=f"实时连通性 · 延迟趋势 · 状态日志      v{APP_VERSION}",
                bg=HEADER_BG,
                fg="#94a3b8",
                font=self.font_sub,
            ).pack(anchor="w", pady=(4, 0))

            stats = tk.Frame(header, bg=HEADER_BG)
            stats.pack(side="right", padx=22)
            self._stat_labels: Dict[str, "tk.Label"] = {}
            cards = [
                ("total", "监测主机", "#e2e8f0"),
                ("up", "在线", "#4ade80"),
                ("down", "离线", "#f87171"),
                ("latency", "平均延迟", "#60a5fa"),
            ]
            for key, caption, color in cards:
                card = tk.Frame(stats, bg=HEADER_CARD, padx=14, pady=6)
                card.pack(side="left", padx=5)
                value = tk.Label(card, text="—", bg=HEADER_CARD, fg=color, font=self.font_card)
                value.pack()
                tk.Label(card, text=caption, bg=HEADER_CARD, fg="#94a3b8", font=self.font_sub).pack()
                self._stat_labels[key] = value

        def _build_toolbar(self) -> None:
            bar = tk.Frame(self.root, bg=BG)
            bar.pack(fill="x", padx=18, pady=(10, 6))
            self._pause_button = self._make_button(bar, "暂停监测", self._toggle_pause)
            self._pause_button.pack(side="left", padx=(0, 8))
            self._make_button(bar, "立即检测", self._probe_now, primary=True).pack(side="left", padx=(0, 8))
            self._make_button(bar, "添加主机", self._add_hosts).pack(side="left", padx=(0, 8))
            self._make_button(bar, "移除主机", self._remove_selected).pack(side="left", padx=(0, 8))
            self._make_button(bar, "监测设置", self._open_settings).pack(side="left", padx=(0, 8))
            self._make_button(bar, "打开日志", self._open_log_dir).pack(side="left", padx=(0, 8))
            tk.Label(
                bar,
                text="双击主机可立即检测",
                bg=BG,
                fg=MUTED,
                font=self.font_sub,
            ).pack(side="right")

        def _make_button(self, parent: "tk.Widget", text: str, command: Callable[[], None], primary: bool = False) -> "tk.Button":
            background = ACCENT if primary else "#ffffff"
            foreground = "#ffffff" if primary else TEXT
            hover = "#1d4ed8" if primary else "#e8eef7"
            button = tk.Button(
                parent,
                text=text,
                command=command,
                bg=background,
                fg=foreground,
                activebackground=hover,
                activeforeground=foreground,
                relief="flat",
                bd=0,
                padx=14,
                pady=7,
                cursor="hand2",
                font=(self.family, 10),
                highlightthickness=1,
                highlightbackground=ACCENT if primary else BORDER,
                highlightcolor=ACCENT if primary else BORDER,
            )
            button.bind("<Enter>", lambda _event: button.configure(bg=hover))
            button.bind("<Leave>", lambda _event: button.configure(bg=background))
            return button

        def _build_statusbar(self) -> None:
            bar = tk.Frame(self.root, bg="#dde5ef", height=26)
            bar.pack(fill="x", side="bottom")
            bar.pack_propagate(False)
            self._status_label = tk.Label(bar, text="正在启动…", bg="#dde5ef", fg="#334155", font=self.font_sub)
            self._status_label.pack(side="left", padx=18)
            self._log_path_label = tk.Label(
                bar,
                text=f"日志：{self.log_dir}",
                bg="#dde5ef",
                fg="#64748b",
                font=self.font_sub,
            )
            self._log_path_label.pack(side="right", padx=18)

        def _build_panes(self) -> None:
            self._pane = ttk.PanedWindow(self.root, orient="vertical")
            self._pane.pack(fill="both", expand=True, padx=18, pady=(0, 8))
            content = tk.Frame(self._pane, bg=BG)
            self._pane.add(content, weight=4)

            self._split = ttk.PanedWindow(content, orient="horizontal")
            self._split.pack(fill="both", expand=True)
            table_card = tk.Frame(self._split, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            detail_card = tk.Frame(self._split, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            self._split.add(table_card, weight=3)
            self._split.add(detail_card, weight=2)

            self._build_table(table_card)
            self._build_detail(detail_card)
            self.root.after(250, self._set_initial_sash)

        def _set_initial_sash(self) -> None:
            try:
                height = self._pane.winfo_height()
                if height > 300:
                    self._pane.sashpos(0, max(320, height - 200))
            except Exception:
                pass

        def _build_table(self, parent: "tk.Frame") -> None:
            head = tk.Frame(parent, bg=CARD)
            head.pack(fill="x", padx=16, pady=(12, 6))
            tk.Label(head, text="主机状态", bg=CARD, fg=TEXT, font=self.font_section).pack(side="left")
            self._table_count = tk.Label(head, text="共 0 台", bg=CARD, fg=MUTED, font=self.font_sub)
            self._table_count.pack(side="left", padx=10)

            wrap = tk.Frame(parent, bg=CARD)
            wrap.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 10))

            columns = ("host", "status", "rtt", "avail", "fails", "changed", "checked")
            self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="extended")
            headings = {
                "host": "主机 / IP",
                "status": "状态",
                "rtt": "延迟",
                "avail": "可用率",
                "fails": "连续失败",
                "changed": "状态变化",
                "checked": "最后检测",
            }
            widths = {"host": 200, "status": 76, "rtt": 80, "avail": 76, "fails": 72, "changed": 92, "checked": 88}
            for column in columns:
                self.tree.heading(column, text=headings[column], anchor="w" if column == "host" else "center")
                self.tree.column(
                    column,
                    width=widths[column],
                    minwidth=52,
                    stretch=True,
                    anchor="w" if column == "host" else "center",
                )

            scrollbar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=scrollbar.set)
            self.tree.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            self.tree.tag_configure("up", foreground=UP)
            self.tree.tag_configure("down", foreground=DOWN, font=(self.family, 10, "bold"))
            self.tree.tag_configure("warn", foreground=WARN)
            self.tree.tag_configure("unknown", foreground=MUTED)

            self._empty_hint = tk.Label(
                wrap,
                text="尚未添加监测主机\n\n点击「添加主机」，或直接编辑 server_list.txt",
                bg=CARD,
                fg=MUTED,
                font=self.font_sub,
                justify="center",
            )

            self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
            self.tree.bind("<Double-1>", self._on_tree_double_click)
            self.tree.bind("<Button-3>", self._show_context_menu)
            self._context_menu = tk.Menu(self.root, tearoff=0)
            self._context_menu.add_command(label="立即检测", command=self._probe_selected)
            self._context_menu.add_command(label="复制主机地址", command=self._copy_selected_host)
            self._context_menu.add_separator()
            self._context_menu.add_command(label="移除该主机", command=self._remove_selected)

        def _build_detail(self, parent: "tk.Frame") -> None:
            top = tk.Frame(parent, bg=CARD)
            top.pack(fill="x", padx=18, pady=(14, 2))
            self._detail_host = tk.Label(top, text="未选择主机", bg=CARD, fg=TEXT, font=(self.family, 13, "bold"))
            self._detail_host.pack(side="left")
            self._status_pill = tk.Label(
                top,
                text="—",
                bg="#e5e7eb",
                fg=MUTED,
                padx=10,
                pady=2,
                font=(self.family, 9, "bold"),
            )
            self._status_pill.pack(side="right")

            self._detail_sub = tk.Label(
                parent,
                text="在左侧列表中选择一台主机查看实时趋势",
                bg=CARD,
                fg=MUTED,
                font=self.font_sub,
                anchor="w",
            )
            self._detail_sub.pack(fill="x", padx=18, pady=(0, 2))

            self._trend = tk.Canvas(parent, bg=CARD, height=140, highlightthickness=0)
            self._trend.pack(fill="both", expand=True, padx=12, pady=(2, 6))
            self._trend.bind("<Configure>", self._on_trend_resize)

            stats = tk.Frame(parent, bg=CARD)
            stats.pack(fill="x", padx=18, pady=(0, 2))
            self._detail_values: Dict[str, "tk.Label"] = {}
            items = [
                ("avail", "可用率"),
                ("avg", "平均延迟"),
                ("min", "最小延迟"),
                ("max", "最大延迟"),
                ("fails", "连续失败"),
                ("samples", "采样次数"),
                ("changed", "最近状态变化"),
                ("checked", "最后检测"),
            ]
            for index, (key, caption) in enumerate(items):
                cell = tk.Frame(stats, bg=CARD)
                cell.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 20), pady=3)
                tk.Label(cell, text=caption, bg=CARD, fg=MUTED, font=self.font_sub).pack(side="left")
                value = tk.Label(cell, text="—", bg=CARD, fg=TEXT, font=(self.family, 10, "bold"))
                value.pack(side="right")
                self._detail_values[key] = value
            stats.columnconfigure(0, weight=1)
            stats.columnconfigure(1, weight=1)

            self._detail_error = tk.Label(
                parent,
                text="",
                bg=CARD,
                fg=MUTED,
                font=self.font_sub,
                wraplength=360,
                justify="left",
                anchor="w",
            )
            self._detail_error.pack(fill="x", padx=18, pady=(4, 10))
            parent.bind(
                "<Configure>",
                lambda event: self._detail_error.configure(wraplength=max(200, event.width - 40)),
            )

        def _build_log_panel(self) -> None:
            card = tk.Frame(self._pane, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            self._pane.add(card, weight=1)
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x", padx=14, pady=(8, 4))
            tk.Label(head, text="事件日志", bg=CARD, fg=TEXT, font=self.font_section).pack(side="left")
            tk.Label(
                head,
                text="仅状态变化时写入日志文件",
                bg=CARD,
                fg=MUTED,
                font=self.font_sub,
            ).pack(side="right", padx=(0, 4))
            self._autoscroll = tk.BooleanVar(value=True)
            tk.Checkbutton(
                head,
                text="自动滚动",
                variable=self._autoscroll,
                bg=CARD,
                activebackground=CARD,
                selectcolor=CARD,
                fg=MUTED,
                font=self.font_sub,
            ).pack(side="right", padx=(0, 10))

            wrap = tk.Frame(card, bg=LOG_BG)
            wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            self.log_text = tk.Text(
                wrap,
                bg=LOG_BG,
                fg="#cbd5e1",
                insertbackground="#cbd5e1",
                relief="flat",
                bd=0,
                font=self.font_mono,
                wrap="word",
                height=5,
                state="disabled",
                padx=10,
                pady=6,
            )
            scrollbar = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=scrollbar.set)
            self.log_text.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            self.log_text.tag_configure("up", foreground="#4ade80")
            self.log_text.tag_configure("down", foreground="#f87171")
            self.log_text.tag_configure("info", foreground="#94a3b8")
            self.log_text.tag_configure("warn", foreground="#fbbf24")
            self.log_text.tag_configure("error", foreground="#fb923c")

        def _bind_shortcuts(self) -> None:
            self.root.bind("<F5>", lambda _event: self._probe_now())
            self.root.bind("<Control-r>", lambda _event: self._reload_hosts())
            self.root.bind("<Control-p>", lambda _event: self._toggle_pause())
            self.root.bind("<Control-q>", lambda _event: self.on_close())
            self.root.bind("<F1>", lambda _event: self._show_about())

        # ------------------------------------------------ 刷新循环
        def _tick(self) -> None:
            if self._shutdown_done:
                return
            try:
                self._drain_events()
                selected = self._selected_host()
                snapshot = self.engine.snapshot(history_host=selected)
                self._snapshot = snapshot
                self._by_host = {item["host"]: item for item in snapshot}
                self._refresh_summary(snapshot)
                self._refresh_table(snapshot)
                self._refresh_detail()
                self._refresh_status(snapshot)
                self._tick_count += 1
                if self._tick_count % 4 == 0:
                    self._check_server_file()
            except Exception:
                append_crash_log(self.log_dir, traceback.format_exc())
                try:
                    self._append_log(f"{now_text()} [ERROR] 界面刷新异常，已记录到 crash.log", "error")
                except Exception:
                    pass
            finally:
                if not self._shutdown_done:
                    self._after_id = self.root.after(REFRESH_MS, self._tick)

        def _drain_events(self) -> None:
            for _ in range(200):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                if event.get("type") == "line":
                    self._append_log(str(event.get("text", "")), str(event.get("level", "info")))

        def _refresh_summary(self, snapshot: Sequence[Dict[str, Any]]) -> None:
            total = len(snapshot)
            up = sum(1 for item in snapshot if item["status"] == "up")
            down = sum(1 for item in snapshot if item["status"] == "down")
            latencies = [item["rtt"] for item in snapshot if item["rtt"] is not None]
            average = sum(latencies) / len(latencies) if latencies else None
            self._stat_labels["total"].configure(text=str(total))
            self._stat_labels["up"].configure(text=str(up))
            self._stat_labels["down"].configure(text=str(down))
            self._stat_labels["latency"].configure(text=f"{average:.0f} ms" if average is not None else "—")

            title = f"{WINDOW_TITLE} — {up}/{total} 在线" if total else WINDOW_TITLE
            if title != self._current_title:
                self._current_title = title
                try:
                    self.root.title(title)
                except Exception:
                    pass

        def _refresh_table(self, snapshot: Sequence[Dict[str, Any]]) -> None:
            present = set()
            for item in snapshot:
                host = item["host"]
                present.add(host)
                status_text, tag, _color = status_display(item)
                availability = item["availability"]
                values = (
                    host,
                    status_text,
                    f"{item['rtt']:.1f} ms" if item["rtt"] is not None else "—",
                    f"{availability * 100:.1f}%" if availability is not None else "—",
                    str(item["consecutive_failures"]),
                    ago_text(item["last_change_at"]),
                    clock_text(item["last_probe_at"]),
                )
                signature = (values, tag)
                if self.tree.exists(host):
                    if self._row_sig.get(host) != signature:
                        self.tree.item(host, values=values, tags=(tag,))
                        self._row_sig[host] = signature
                else:
                    self.tree.insert("", "end", iid=host, values=values, tags=(tag,))
                    self._row_sig[host] = signature
            for host in list(self._row_sig.keys()):
                if host not in present:
                    try:
                        self.tree.delete(host)
                    except Exception:
                        pass
                    self._row_sig.pop(host, None)
            if snapshot and not self.tree.selection():
                try:
                    self.tree.selection_set(snapshot[0]["host"])
                except Exception:
                    pass
            self._table_count.configure(text=f"共 {len(snapshot)} 台")

        def _refresh_detail(self) -> None:
            host = self._selected_host()
            if host is None or host not in self._by_host:
                self._detail_host.configure(text="未选择主机")
                self._status_pill.configure(text="—", bg="#e5e7eb", fg=MUTED)
                self._detail_sub.configure(text="在左侧列表中选择一台主机查看实时趋势")
                for label in self._detail_values.values():
                    label.configure(text="—")
                self._detail_error.configure(text="", fg=MUTED)
                self._current_history = []
                self._draw_trend([])
                self._detail_sig = None
                return

            item = self._by_host[host]
            status_text, _tag, color = status_display(item)
            self._detail_host.configure(text=host)
            pill_bg = {"up": "#dcfce7", "down": "#fee2e2", "warn": "#fef3c7", "unknown": "#e5e7eb"}[_tag]
            self._status_pill.configure(text=status_text.replace("● ", "").replace("◌ ", "").replace("○ ", ""), bg=pill_bg, fg=color)
            self._detail_sub.configure(
                text=f"延迟趋势 · 最近 {item['history_len']} 次采样 · 每 {self.config.interval:g} 秒一次"
            )

            rtts = [rtt for _ts, rtt in item["history"] if rtt is not None]
            average = sum(rtts) / len(rtts) if rtts else None
            availability = item["availability"]
            self._detail_values["avail"].configure(
                text=f"{availability * 100:.1f}%" if availability is not None else "—"
            )
            self._detail_values["avg"].configure(text=f"{average:.1f} ms" if average is not None else "—")
            self._detail_values["min"].configure(text=f"{min(rtts):.1f} ms" if rtts else "—")
            self._detail_values["max"].configure(text=f"{max(rtts):.1f} ms" if rtts else "—")
            self._detail_values["fails"].configure(text=str(item["consecutive_failures"]))
            self._detail_values["samples"].configure(text=str(item["samples"]))
            self._detail_values["changed"].configure(text=datetime_text(item["last_change_at"]))
            self._detail_values["checked"].configure(text=clock_text(item["last_probe_at"]))

            error = item["last_error"]
            if error and item["status"] == "down":
                self._detail_error.configure(text=f"最近错误：{error}（{ago_text(item['last_change_at'])}）", fg=DOWN)
            elif error:
                self._detail_error.configure(text=f"最近一次失败：{error}", fg=MUTED)
            else:
                self._detail_error.configure(text="", fg=MUTED)

            signature = (host, item["history_len"], item["last_probe_at"], item["status"])
            if signature != self._detail_sig:
                self._detail_sig = signature
                self._current_history = item["history"]
                self._draw_trend(item["history"])

        def _refresh_status(self, snapshot: Sequence[Dict[str, Any]]) -> None:
            total = len(snapshot)
            up = sum(1 for item in snapshot if item["status"] == "up")
            if not total:
                state = "待添加主机"
            elif self.engine.is_paused:
                state = "已暂停"
            else:
                state = "监测中"
            text = (
                f"{state} · 间隔 {self.config.interval:g}s · 超时 {self.config.timeout:g}s"
                f" · 在线 {up}/{total} · 连续失败 {self.config.fail_threshold} 次判离线"
            )
            self._status_label.configure(text=text)
            self._pause_button.configure(text="继续监测" if self.engine.is_paused else "暂停监测")
            self._refresh_empty_hint()

        def _refresh_empty_hint(self) -> None:
            if self.hosts:
                self._empty_hint.place_forget()
            else:
                self._empty_hint.place(relx=0.5, rely=0.5, anchor="center")
                self._empty_hint.lift()

        # ------------------------------------------------ 趋势图
        def _on_trend_resize(self, _event: Any = None) -> None:
            self._draw_trend(self._current_history)

        def _draw_trend(self, history: Sequence[Tuple[float, Optional[float]]]) -> None:
            canvas = self._trend
            canvas.delete("all")
            width = canvas.winfo_width()
            height = canvas.winfo_height()
            if width < 80 or height < 60:
                return
            if not history:
                canvas.create_text(
                    width / 2,
                    height / 2,
                    text="暂无采样数据",
                    fill=MUTED,
                    font=self.font_sub,
                )
                return

            left, right, top, bottom = 46, 10, 12, 22
            plot_w = max(1, width - left - right)
            plot_h = max(1, height - top - bottom)
            values = [rtt for _ts, rtt in history if rtt is not None]
            peak = max(values) if values else 5.0
            peak = max(peak * 1.25, 5.0)

            for index in range(5):
                fraction = index / 4
                y = top + plot_h * fraction
                canvas.create_line(left, y, width - right, y, fill="#e5e7eb")
                canvas.create_text(
                    left - 6,
                    y,
                    text=f"{peak * (1 - fraction):.0f}",
                    anchor="e",
                    fill="#94a3b8",
                    font=self.font_tiny,
                )

            count = len(history)
            points: List[Tuple[float, float]] = []
            for index, (_timestamp, rtt) in enumerate(history):
                x = left + (plot_w * index / (count - 1) if count > 1 else plot_w / 2)
                if rtt is None:
                    y = top + plot_h - 3
                    canvas.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=DOWN, outline="")
                    points.append((x, y))
                else:
                    y = top + plot_h * (1 - min(rtt / peak, 1.0))
                    points.append((x, y))

            previous: Optional[Tuple[float, float]] = None
            previous_was_rtt = False
            for (x, y), (_timestamp, rtt) in zip(points, history):
                if rtt is not None:
                    if previous is not None and previous_was_rtt:
                        canvas.create_line(previous[0], previous[1], x, y, fill=ACCENT, width=2)
                    previous = (x, y)
                    previous_was_rtt = True
                else:
                    previous = None
                    previous_was_rtt = False

            last_rtt_point = None
            for (x, y), (_timestamp, rtt) in zip(points, history):
                if rtt is not None:
                    last_rtt_point = (x, y)
            if last_rtt_point is not None:
                canvas.create_oval(
                    last_rtt_point[0] - 3.5,
                    last_rtt_point[1] - 3.5,
                    last_rtt_point[0] + 3.5,
                    last_rtt_point[1] + 3.5,
                    fill=ACCENT,
                    outline=CARD,
                    width=1,
                )

            canvas.create_text(left, height - 6, text=clock_text(history[0][0]), anchor="sw", fill="#94a3b8", font=self.font_tiny)
            if count > 1:
                canvas.create_text(
                    width - right,
                    height - 6,
                    text=clock_text(history[-1][0]),
                    anchor="se",
                    fill="#94a3b8",
                    font=self.font_tiny,
                )

        # ------------------------------------------------ 日志面板
        def _append_log(self, text: str, level: str = "info") -> None:
            if not text:
                return
            widget = self.log_text
            try:
                at_bottom = widget.yview()[1] >= 0.999
            except Exception:
                at_bottom = True
            widget.configure(state="normal")
            widget.insert("end", text + "\n", (level,))
            try:
                lines = int(widget.index("end-1c").split(".")[0])
                if lines > MAX_LOG_LINES:
                    widget.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
            except Exception:
                pass
            widget.configure(state="disabled")
            if at_bottom or self._autoscroll.get():
                widget.see("end")

        # ------------------------------------------------ 交互
        def _selected_host(self) -> Optional[str]:
            try:
                selection = self.tree.selection()
            except Exception:
                return None
            if not selection:
                return None
            return selection[0]

        def _on_tree_select(self, _event: Any = None) -> None:
            self._detail_sig = None

        def _on_tree_double_click(self, event: Any) -> None:
            row = self.tree.identify_row(event.y)
            if not row:
                return
            self.tree.selection_set(row)
            self.engine.probe_now(row)

        def _show_context_menu(self, event: Any) -> None:
            row = self.tree.identify_row(event.y)
            if not row:
                return
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            try:
                self._context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._context_menu.grab_release()

        def _copy_selected_host(self) -> None:
            host = self._selected_host()
            if host is None:
                return
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(host)
            except Exception:
                pass

        def _probe_now(self) -> None:
            self.engine.probe_now()
            self._append_log(f"{now_text()} [INFO] 已触发一轮立即检测", "info")

        def _probe_selected(self) -> None:
            host = self._selected_host()
            if host is None:
                messagebox.showinfo("提示", "请先在列表中选择一台主机。", parent=self.root)
                return
            self.engine.probe_now(host)
            self._append_log(f"{now_text()} [INFO] 已触发 {host} 立即检测", "info")

        def _toggle_pause(self) -> None:
            if self.engine.is_paused:
                self.engine.resume()
                line = self.logger.write("INFO", "监测已恢复")
                self._append_log(line, "info")
            else:
                self.engine.pause()
                line = self.logger.write("INFO", "监测已暂停")
                self._append_log(line, "warn")

        def _add_hosts(self) -> None:
            raw = simpledialog.askstring(
                "添加监测主机",
                "输入 IP 地址或域名，多个可用逗号、空格分隔：\n（支持 IPv4 / IPv6 / 主机名）",
                parent=self.root,
            )
            if not raw:
                return
            tokens = [token for token in re.split(r"[\s,;，；]+", raw.strip()) if token]
            valid: List[str] = []
            invalid: List[str] = []
            existing = {host.lower() for host in self.hosts}
            for token in tokens:
                host = normalize_host(token)
                if host is None:
                    invalid.append(token)
                elif host.lower() not in existing and host not in valid:
                    valid.append(host)
            if invalid:
                messagebox.showwarning(
                    "部分输入无效",
                    "以下内容不是有效的 IP 或域名，已忽略：\n" + "\n".join(invalid),
                    parent=self.root,
                )
            if not valid:
                return
            self._apply_hosts(self.hosts + valid, save=True, reason="手动添加")

        def _remove_selected(self) -> None:
            selection = list(self.tree.selection())
            if not selection:
                messagebox.showinfo("提示", "请先在列表中选择要移除的主机。", parent=self.root)
                return
            if not messagebox.askyesno(
                "确认移除",
                "确定不再监测以下主机？\n\n" + "\n".join(selection),
                parent=self.root,
            ):
                return
            self._apply_hosts([host for host in self.hosts if host not in selection], save=True, reason="手动移除")

        def _apply_hosts(self, hosts: Sequence[str], save: bool, reason: str) -> bool:
            hosts = unique_hosts(hosts)
            if save:
                try:
                    save_servers(self.server_file, hosts)
                    self._server_mtime = self.server_file.stat().st_mtime
                except OSError as exc:
                    messagebox.showerror("保存失败", f"无法写入 {self.server_file}：\n{exc}", parent=self.root)
                    return False
            added, removed = self.engine.set_hosts(hosts)
            self.hosts = hosts
            if added or removed:
                parts = []
                if added:
                    parts.append("新增 " + ", ".join(added))
                if removed:
                    parts.append("移除 " + ", ".join(removed))
                line = self.logger.write("INFO", f"{reason}：" + "；".join(parts))
                self._append_log(line, "info")
            self._refresh_empty_hint()
            return True

        def _reload_hosts(self) -> None:
            hosts, invalid = load_servers(self.server_file)
            if invalid:
                self._append_log(f"{now_text()} [WARN] 忽略无效主机：{', '.join(invalid)}", "warn")
            try:
                self._server_mtime = self.server_file.stat().st_mtime
            except OSError:
                self._server_mtime = None
            if hosts == self.hosts:
                self._append_log(f"{now_text()} [INFO] 主机列表无变化", "info")
                return
            self._apply_hosts(hosts, save=False, reason="重新加载主机列表")

        def _check_server_file(self) -> None:
            try:
                mtime = self.server_file.stat().st_mtime
            except OSError:
                return
            if self._server_mtime is None:
                self._server_mtime = mtime
                return
            if abs(mtime - self._server_mtime) < 0.001:
                return
            self._server_mtime = mtime
            hosts, invalid = load_servers(self.server_file)
            if invalid:
                self._append_log(f"{now_text()} [WARN] 忽略无效主机：{', '.join(invalid)}", "warn")
            if hosts == self.hosts:
                return
            self._apply_hosts(hosts, save=False, reason="检测到 server_list.txt 变更")

        def _open_log_dir(self) -> None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(str(self.log_dir))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(self.log_dir)])
                else:
                    subprocess.Popen(["xdg-open", str(self.log_dir)])
            except Exception as exc:
                messagebox.showerror("无法打开日志目录", str(exc), parent=self.root)

        def _show_about(self) -> None:
            messagebox.showinfo(
                f"关于 {APP_NAME}",
                f"{APP_NAME} v{APP_VERSION}\n\n"
                "功能：实时监测服务器连通性与延迟，状态变化写入日志。\n\n"
                f"配置文件：{self.config_path}\n"
                f"主机列表：{self.server_file}\n"
                f"日志目录：{self.log_dir}\n\n"
                "快捷键：F5 立即检测 · Ctrl+R 重载列表 · Ctrl+P 暂停/继续 · Ctrl+Q 退出",
                parent=self.root,
            )

        # ------------------------------------------------ 设置
        def _open_settings(self) -> None:
            if self._settings_win is not None and self._settings_win.winfo_exists():
                self._settings_win.lift()
                self._settings_win.focus_force()
                return

            win = tk.Toplevel(self.root)
            self._settings_win = win
            win.title("监测设置")
            win.configure(bg=CARD)
            win.resizable(False, False)
            win.transient(self.root)
            win.grab_set()

            body = tk.Frame(win, bg=CARD)
            body.pack(fill="both", expand=True, padx=24, pady=18)
            tk.Label(body, text="监测参数", bg=CARD, fg=TEXT, font=self.font_section).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
            )

            fields = [
                ("interval", "监测间隔（秒）", f"{self.config.interval:g}", "1 – 3600"),
                ("timeout", "单次超时（秒）", f"{self.config.timeout:g}", "0.5 – 30"),
                ("max_concurrent", "并发探测上限", str(self.config.max_concurrent), "1 – 64"),
                ("fail_threshold", "连续失败判离线", str(self.config.fail_threshold), "1 – 10"),
                ("heartbeat_minutes", "状态心跳（分钟）", str(self.config.heartbeat_minutes), "0 表示关闭"),
                ("retain_days", "日志保留（天）", str(self.config.retain_days), "0 表示永久"),
            ]
            variables: Dict[str, "tk.StringVar"] = {}
            for index, (key, caption, value, hint) in enumerate(fields, start=1):
                tk.Label(body, text=caption, bg=CARD, fg=TEXT, font=(self.family, 10)).grid(
                    row=index, column=0, sticky="w", pady=6
                )
                variable = tk.StringVar(value=value)
                variables[key] = variable
                entry = tk.Entry(
                    body,
                    textvariable=variable,
                    width=10,
                    font=(self.family, 10),
                    relief="solid",
                    bd=1,
                    highlightthickness=0,
                )
                entry.grid(row=index, column=1, sticky="w", padx=(18, 10), pady=6)
                tk.Label(body, text=hint, bg=CARD, fg=MUTED, font=self.font_sub).grid(
                    row=index, column=2, sticky="w", pady=6
                )

            note = tk.Label(
                body,
                text="修改后立即生效；并发上限用于限制同时运行的 ping 进程数量。",
                bg=CARD,
                fg=MUTED,
                font=self.font_sub,
            )
            note.grid(row=len(fields) + 1, column=0, columnspan=3, sticky="w", pady=(10, 0))

            buttons = tk.Frame(win, bg=CARD)
            buttons.pack(fill="x", padx=24, pady=(0, 18))
            self._make_button(buttons, "取消", lambda: self._close_settings(win)).pack(side="right", padx=(8, 0))
            self._make_button(buttons, "保存设置", lambda: self._save_settings(win, variables), primary=True).pack(side="right")

            win.protocol("WM_DELETE_WINDOW", lambda: self._close_settings(win))
            win.update_idletasks()
            width = win.winfo_reqwidth()
            height = win.winfo_reqheight()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 3)
            win.geometry(f"+{x}+{y}")
            win.focus_force()

        def _close_settings(self, win: "tk.Toplevel") -> None:
            try:
                win.grab_release()
                win.destroy()
            except Exception:
                pass
            if self._settings_win is win:
                self._settings_win = None

        def _save_settings(self, win: "tk.Toplevel", variables: Dict[str, "tk.StringVar"]) -> None:
            errors: List[str] = []
            try:
                interval = float(variables["interval"].get())
                if not 1 <= interval <= 3600:
                    errors.append("监测间隔必须在 1–3600 秒之间")
            except ValueError:
                interval = self.config.interval
                errors.append("监测间隔必须是数字")
            try:
                timeout = float(variables["timeout"].get())
                if not 0.5 <= timeout <= 30:
                    errors.append("超时必须在 0.5–30 秒之间")
            except ValueError:
                timeout = self.config.timeout
                errors.append("超时必须是数字")
            try:
                max_concurrent = int(variables["max_concurrent"].get())
                if not 1 <= max_concurrent <= 64:
                    errors.append("并发上限必须在 1–64 之间")
            except ValueError:
                max_concurrent = self.config.max_concurrent
                errors.append("并发上限必须是整数")
            try:
                fail_threshold = int(variables["fail_threshold"].get())
                if not 1 <= fail_threshold <= 10:
                    errors.append("连续失败次数必须在 1–10 之间")
            except ValueError:
                fail_threshold = self.config.fail_threshold
                errors.append("连续失败次数必须是整数")
            try:
                heartbeat = int(variables["heartbeat_minutes"].get())
                if not 0 <= heartbeat <= 1440:
                    errors.append("状态心跳必须在 0–1440 分钟之间")
            except ValueError:
                heartbeat = self.config.heartbeat_minutes
                errors.append("状态心跳必须是整数")
            try:
                retain_days = int(variables["retain_days"].get())
                if not 0 <= retain_days <= 3650:
                    errors.append("日志保留天数必须在 0–3650 之间")
            except ValueError:
                retain_days = self.config.retain_days
                errors.append("日志保留天数必须是整数")

            if errors:
                messagebox.showerror("设置未保存", "\n".join(errors), parent=win)
                return

            self.config.interval = max(1.0, interval)
            self.config.timeout = max(0.5, timeout)
            self.config.max_concurrent = max(1, max_concurrent)
            self.config.fail_threshold = max(1, fail_threshold)
            self.config.heartbeat_minutes = max(0, heartbeat)
            self.config.retain_days = max(0, retain_days)
            try:
                self.config.save(self.config_path)
            except OSError as exc:
                messagebox.showerror("保存失败", f"无法写入配置文件：\n{exc}", parent=win)
                return
            self.logger.set_retention(self.config.retain_days)
            self.engine.update_concurrency()
            line = self.logger.write(
                "INFO",
                f"监测参数已更新：间隔 {self.config.interval:g}s · 超时 {self.config.timeout:g}s"
                f" · 并发 {self.config.max_concurrent} · 连续失败 {self.config.fail_threshold} 次判离线",
            )
            self._append_log(line, "info")
            self._close_settings(win)

        # ------------------------------------------------ 异常与退出
        def _tk_callback_error(self, exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
            text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            append_crash_log(self.log_dir, text)
            try:
                self._append_log(f"{now_text()} [ERROR] 界面操作异常：{exc_value}", "error")
            except Exception:
                pass
            try:
                messagebox.showerror(
                    "程序错误",
                    f"界面操作出现异常：\n{exc_value}\n\n详细信息已写入 crash.log，程序会继续运行。",
                    parent=self.root,
                )
            except Exception:
                pass

        def _save_geometry(self) -> None:
            try:
                if self.root.state() == "normal":
                    geometry = self.root.geometry()
                    if GEOMETRY_RE.match(geometry):
                        self.config.window_geometry = geometry
            except Exception:
                pass

        def shutdown(self) -> None:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            if self._after_id is not None:
                try:
                    self.root.after_cancel(self._after_id)
                except Exception:
                    pass
                self._after_id = None
            self._save_geometry()
            try:
                self.config.save(self.config_path)
            except Exception as exc:
                try:
                    self.logger.write("WARN", f"保存配置失败：{exc}")
                except Exception:
                    pass
            try:
                self.engine.stop(timeout=2.0)
            except Exception:
                pass
            try:
                self.logger.write("INFO", f"监测已停止（共 {len(self.hosts)} 个目标）")
            except Exception:
                pass
            self.logger.close()

        def on_close(self) -> None:
            if self._closing:
                return
            self._closing = True
            self.shutdown()
            try:
                self.root.destroy()
            except Exception:
                pass


def status_display(item: Dict[str, Any]) -> Tuple[str, str, str]:
    """返回 (显示文本, 标签, 颜色)。"""
    status = item["status"]
    failures = item["consecutive_failures"]
    if status == "up":
        return "● 在线", "up", UP
    if status == "down":
        return "● 离线", "down", DOWN
    if failures:
        return "◌ 检测中", "warn", WARN
    return "○ 等待", "unknown", MUTED


# ============================ 无界面模式 ==================================

def format_summary(snapshot: Sequence[Dict[str, Any]]) -> str:
    total = len(snapshot)
    up = sum(1 for item in snapshot if item["status"] == "up")
    down = sum(1 for item in snapshot if item["status"] == "down")
    latencies = [item["rtt"] for item in snapshot if item["rtt"] is not None]
    average = sum(latencies) / len(latencies) if latencies else None
    parts = [f"{up}/{total} 在线"]
    if down:
        parts.append(f"{down} 离线")
    if average is not None:
        parts.append(f"平均延迟 {average:.0f} ms")
    return "，".join(parts)


def run_headless(engine: MonitorEngine, logger: EventLogger, duration: Optional[float]) -> int:
    engine.start()
    snapshot = engine.snapshot()
    if not snapshot:
        logger.write("WARN", "没有配置任何监测主机，请编辑 server_list.txt 或使用图形界面添加")
        safe_print("没有配置任何监测主机，无界面模式将空闲等待；按 Ctrl+C 退出。")
    else:
        logger.write("INFO", f"无界面模式启动，共 {len(snapshot)} 个目标")
        safe_print(f"{now_text()}  无界面监测已启动，共 {len(snapshot)} 个目标；按 Ctrl+C 退出。")
    started = time.monotonic()
    next_summary = started
    try:
        while True:
            time.sleep(0.5)
            current = time.monotonic()
            if duration is not None and current - started >= max(0.0, duration):
                break
            if current >= next_summary:
                next_summary = current + SUMMARY_SECONDS
                snapshot = engine.snapshot()
                safe_print(f"{now_text()}  摘要：{format_summary(snapshot)}")
    except KeyboardInterrupt:
        safe_print("\n收到中断信号，正在停止…")
    finally:
        engine.stop()
        logger.write("INFO", "监测已停止")
        logger.close()
    return 0


def run_once(hosts: Sequence[str], config: Config) -> int:
    if not hosts:
        safe_print("没有配置任何监测主机，请编辑 server_list.txt 后重试。")
        return 2
    safe_print(f"单次连通性检测：{len(hosts)} 个目标，超时 {config.timeout:g} 秒")
    safe_print("-" * 64)
    results: Dict[str, ProbeResult] = {}
    workers = min(16, max(1, len(hosts)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ping_once, host, config.timeout): host for host in hosts}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:  # 理论上不会发生，但保持健壮
                host = futures[future]
                result = ProbeResult(host, False, None, time.time(), str(exc))
            results[result.host] = result

    up = 0
    latencies: List[float] = []
    for host in hosts:
        result = results.get(host)
        if result is None:
            safe_print(f"  {host:<30} 未知   未返回结果")
            continue
        if result.ok:
            up += 1
            if result.rtt is not None:
                latencies.append(result.rtt)
            safe_print(f"  {host:<30} 在线   {result.rtt:.1f} ms")
        else:
            safe_print(f"  {host:<30} 离线   {result.error}")
    average = sum(latencies) / len(latencies) if latencies else None
    safe_print("-" * 64)
    tail = f"，平均延迟 {average:.0f} ms" if average is not None else ""
    safe_print(f"合计：{up}/{len(hosts)} 在线{tail}")
    return 0 if up == len(hosts) else 1


# ============================ 启动逻辑 ====================================

class SingleInstance:
    """Windows 命名互斥量，防止重复启动导致重复写日志。"""

    def __init__(self, name: str = "PingMonitorV2.SingleInstance") -> None:
        self.name = name
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                return True
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                return False
            self._handle = handle
            return True
        except Exception:
            return True

    def release(self) -> None:
        if os.name != "nt" or not self._handle:
            return
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        except Exception:
            pass
        self._handle = None


def focus_existing_window() -> None:
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            for title in (f"{WINDOW_TITLE} — 0/0 在线",):
                hwnd = user32.FindWindowW(None, title)
                if hwnd:
                    break
        if hwnd:
            user32.ShowWindow(ctypes.c_void_p(hwnd), 9)
            user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    except Exception:
        pass


def notify_already_running(headless: bool) -> None:
    if not headless:
        focus_existing_window()
    message = "监测程序已经在运行。\n如需同时运行多个实例，请加 --allow-multi 参数。"
    if HAS_TK and not headless:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("已在运行", message)
            root.destroy()
            return
        except Exception:
            pass
    safe_print(message.replace("\n", " "))


def run_gui(
    config: Config,
    config_path: Path,
    server_file: Path,
    hosts: Sequence[str],
    engine: MonitorEngine,
    logger: EventLogger,
    events: "queue.Queue[Dict[str, Any]]",
    config_warning: Optional[str],
) -> int:
    if not HAS_TK:
        safe_print("当前环境缺少 tkinter，无法启动图形界面；请使用 --headless 运行。")
        return 1
    enable_dpi_awareness()
    root = tk.Tk()
    app = MonitorApp(root, config, config_path, server_file, hosts, engine, logger, events, config_warning)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    try:
        root.mainloop()
    finally:
        app.shutdown()
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="服务器网络连通性实时监测工具",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--headless", action="store_true", help="无图形界面运行，适合服务器或计划任务")
    parser.add_argument("--once", action="store_true", help="只做一轮检测，打印结果后退出")
    parser.add_argument("--duration", type=float, default=None, metavar="SECONDS", help="无界面模式下运行指定秒数后退出（自检用）")
    parser.add_argument("--config", default="config.json", help="配置文件路径（相对程序目录）")
    parser.add_argument("--allow-multi", action="store_true", help="允许同时运行多个实例")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    args = parse_args(argv)
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path
    config_path = config_path.resolve()

    config, config_warning = load_config(config_path)
    server_file = resolve_path(config.server_file)
    log_dir = resolve_path(config.log_dir)

    guard = SingleInstance()
    if not args.allow_multi and not args.once and not guard.acquire():
        notify_already_running(headless=bool(args.headless))
        return 0

    events: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    def report_log_error(message: str) -> None:
        text = f"{now_text()} [ERROR] {message}"
        events.put({"type": "line", "level": "error", "text": text})
        if args.headless or args.once:
            safe_print(text)

    logger = EventLogger(
        log_dir,
        retain_days=config.retain_days,
        echo=bool(args.headless),
        on_error=report_log_error,
    )
    logger.prepare()
    install_exception_hooks(log_dir)

    if not args.once:
        logger.write("INFO", f"{APP_NAME} v{APP_VERSION} 启动（PID {os.getpid()}）")
    if config_warning:
        logger.write("WARN", config_warning)

    hosts, invalid_hosts = load_servers(server_file)
    if invalid_hosts:
        logger.write("WARN", f"忽略无效主机：{', '.join(invalid_hosts)}")

    engine = MonitorEngine(hosts, config, logger, events.put)
    exit_code = 0
    try:
        if args.once:
            exit_code = run_once(hosts, config)
        elif args.headless or not HAS_TK:
            if not HAS_TK and not args.headless:
                safe_print("检测到 tkinter 不可用，自动切换为无界面模式。")
            exit_code = run_headless(engine, logger, args.duration)
        else:
            exit_code = run_gui(
                config,
                config_path,
                server_file,
                hosts,
                engine,
                logger,
                events,
                config_warning,
            )
    except KeyboardInterrupt:
        safe_print("\n已取消。")
    except Exception:
        text = traceback.format_exc()
        append_crash_log(log_dir, text)
        logger.write("ERROR", f"程序异常退出：{text.strip().splitlines()[-1]}")
        safe_print(text)
        exit_code = 1
    finally:
        engine.stop()
        logger.close()
        guard.release()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
