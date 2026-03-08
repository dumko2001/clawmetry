"""
clawmetry/sync.py — Cloud sync daemon for clawmetry connect.

Reads local OpenClaw sessions/logs, encrypts with AES-256-GCM (E2E),
and streams to ingest.clawmetry.com. The encryption key never leaves
the local machine — cloud stores ciphertext only.
"""
from __future__ import annotations
import json
import os
import sys
import time
import glob
import base64
import secrets
import logging
import platform
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

INGEST_URL = os.environ.get("CLAWMETRY_INGEST_URL", "https://ingest.clawmetry.com")
CONFIG_DIR  = Path.home() / ".clawmetry"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE  = CONFIG_DIR / "sync-state.json"
LOG_FILE    = CONFIG_DIR / "sync.log"

POLL_INTERVAL = 15    # seconds between sync cycles
STREAM_INTERVAL = 2   # seconds between real-time stream pushes
BATCH_SIZE    = 10    # events per encrypted POST

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("clawmetry-sync")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s [clawmetry-sync] %(levelname)s %(message)s")
    # Detect if stdout is already redirected to our log file (e.g. launchd).
    # In that case, only use StreamHandler to avoid duplicate lines.
    _stdout_is_log = False
    try:
        import os as _os
        if hasattr(sys.stdout, "fileno"):
            _stdout_is_log = _os.path.samefile(
                _os.fstat(sys.stdout.fileno()).st_ino and f"/proc/self/fd/{sys.stdout.fileno()}" or "",
                str(LOG_FILE),
            ) if _os.path.exists(str(LOG_FILE)) else False
    except Exception:
        try:
            _stdout_stat = _os.fstat(sys.stdout.fileno())
            _log_stat = _os.stat(str(LOG_FILE))
            _stdout_is_log = (_stdout_stat.st_dev == _log_stat.st_dev and _stdout_stat.st_ino == _log_stat.st_ino)
        except Exception:
            pass
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)
    if not _stdout_is_log:
        try:
            _fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
            _fh.setFormatter(_fmt)
            log.addHandler(_fh)
        except Exception:
            pass
    log.propagate = False


# ── Encryption (AES-256-GCM) ─────────────────────────────────────────────────

def generate_encryption_key() -> str:
    """Generate a new 256-bit key. Returns base64url string."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _get_aesgcm(key_b64: str):
    """Return an AESGCM cipher from a base64url key."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.urlsafe_b64decode(key_b64 + "==")
        return AESGCM(raw)
    except ImportError:
        raise RuntimeError(
            "E2E encryption requires the 'cryptography' package.\n"
            "  pip install cryptography"
        )


def encrypt_payload(data: dict, key_b64: str) -> str:
    """
    Encrypt a dict as AES-256-GCM.
    Returns base64url(nonce || ciphertext) — a single opaque string.
    Cloud stores this blob and never sees plaintext.
    """
    cipher = _get_aesgcm(key_b64)
    nonce  = secrets.token_bytes(12)          # 96-bit nonce (GCM standard)
    plain  = json.dumps(data).encode()
    ct     = cipher.encrypt(nonce, plain, None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_payload(blob: str, key_b64: str) -> dict:
    """Decrypt a blob produced by encrypt_payload. Used by clients."""
    cipher = _get_aesgcm(key_b64)
    raw    = base64.urlsafe_b64decode(blob + "==")
    nonce, ct = raw[:12], raw[12:]
    return json.loads(cipher.decrypt(nonce, ct, None))


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"No config at {CONFIG_FILE}. Run: clawmetry connect")
    return json.loads(CONFIG_FILE.read_text())


def save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    CONFIG_FILE.chmod(0o600)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_event_ids": {}, "last_log_offsets": {}, "last_sync": None}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _post(path: str, payload: dict, api_key: str, timeout: int = 45) -> dict:
    url  = INGEST_URL.rstrip("/") + path
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-Api-Key": api_key}
    if payload.get("node_id"):
        headers["X-Node-Id"] = payload["node_id"]
    req  = urllib.request.Request(
        url, data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url}: {e.read().decode()[:200]}")


def _get_machine_id() -> str:
    """Stable unique machine identifier (survives reboots and hostname changes)."""
    import uuid as _uuid_mod, hashlib as _hl, subprocess as _sp
    # macOS: IOPlatformUUID
    if platform.system() == "Darwin":
        try:
            r = _sp.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                        capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    parts = line.split('"')
                    if len(parts) >= 2:
                        return parts[-2]
        except Exception:
            pass
    # Linux: /etc/machine-id
    for p in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            mid = open(p).read().strip()
            if mid and len(mid) > 8:
                return mid
        except Exception:
            pass
    # Fallback: stable hash of MAC address
    mac = _uuid_mod.getnode()
    return _hl.sha256(str(mac).encode()).hexdigest()[:32]


def _get_node_metadata() -> dict:
    """Collect rich physical node info for multi-node fleet management."""
    import socket as _sock, multiprocessing as _mp
    meta = {
        "machine_id": _get_machine_id(),
        "hostname": _sock.gethostname(),
        "os": platform.system(),
        "os_version": platform.version()[:120],
        "os_release": platform.release(),
        "arch": platform.machine(),
        "processor": platform.processor()[:80] or platform.machine(),
        "python": platform.python_version(),
    }
    # CPU count
    try:
        meta["cpu_count"] = _mp.cpu_count()
    except Exception:
        pass
    # RAM
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    meta["ram_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except Exception:
        try:
            import subprocess as _sp2
            r = _sp2.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                meta["ram_gb"] = round(int(r.stdout.strip()) / 1024 ** 3, 1)
        except Exception:
            pass
    # Local IPs
    try:
        ips = set()
        try:
            infos = _sock.getaddrinfo(_sock.gethostname(), None)
            for info in infos:
                ip = info[4][0]
                if not ip.startswith("127.") and ":" not in ip:
                    ips.add(ip)
        except Exception:
            pass
        if not ips:
            s2 = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            s2.connect(("8.8.8.8", 80))
            ips.add(s2.getsockname()[0])
            s2.close()
        meta["local_ips"] = sorted(ips)
    except Exception:
        meta["local_ips"] = []
    # MAC address
    try:
        import uuid as _u2
        mac = _u2.getnode()
        meta["mac"] = ":".join(f"{(mac >> i) & 0xff:02x}" for i in range(40, -1, -8))
    except Exception:
        pass
    # clawmetry version
    meta["clawmetry_version"] = _get_version()
    return meta


def validate_key(api_key: str, hostname: str = "", existing_node_id: str = "") -> dict:
    meta = _get_node_metadata()
    if hostname:
        meta["hostname"] = hostname  # CLI override takes priority
    return _post("/auth", {
        "api_key": api_key,
        "hostname": meta["hostname"],
        "machine_id": meta["machine_id"],
        "node_meta": meta,
        "existing_node_id": existing_node_id,
    }, api_key)


# ── Path detection ─────────────────────────────────────────────────────────────


def _find_openclaw_dirs(root, max_depth=4):
    """Search a directory tree for OpenClaw sessions and workspace dirs."""
    sessions_dir = None
    workspace_dir = None
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.replace(root, "").count(os.sep)
            if depth > max_depth:
                dirnames.clear()
                continue
            # Skip noisy dirs
            base = os.path.basename(dirpath)
            if base in ("node_modules", ".git", "__pycache__", "venv", ".venv"):
                dirnames.clear()
                continue
            if dirpath.endswith(os.sep + "agents" + os.sep + "main" + os.sep + "sessions") or                dirpath.endswith("/agents/main/sessions"):
                if not sessions_dir:
                    sessions_dir = dirpath
                    log.info(f"  Found sessions: {dirpath}")
            if os.path.basename(dirpath) == "workspace" and os.path.isfile(os.path.join(dirpath, "AGENTS.md")):
                if not workspace_dir:
                    workspace_dir = dirpath
                    log.info(f"  Found workspace: {dirpath}")
            if sessions_dir and workspace_dir:
                break
    except PermissionError:
        pass
    return sessions_dir, workspace_dir


def _detect_docker_openclaw() -> dict:
    """Auto-detect OpenClaw running in Docker and find its data paths on the host."""
    import subprocess, json as _json
    result = {}
    try:
        # Find containers with openclaw/clawd in name or image
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}}	{{.Names}}	{{.Image}}	{{.Mounts}}"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return {}
        for line in out.stdout.strip().splitlines():
            parts = line.split("	")
            if len(parts) < 3:
                continue
            cid, name, image = parts[0], parts[1], parts[2]
            if not any(k in (name + image).lower() for k in ["openclaw", "clawd", "claw"]):
                continue
            log.info(f"Found OpenClaw Docker container: {name} ({image}) id={cid}")
            # Get volume mounts via docker inspect
            try:
                insp = subprocess.run(
                    ["docker", "inspect", "--format", "{{json .Mounts}}", cid],
                    capture_output=True, text=True, timeout=5)
                mounts = _json.loads(insp.stdout.strip()) if insp.returncode == 0 else []
                for m in mounts:
                    src = m.get("Source", "")
                    dst = m.get("Destination", "")
                    # Look for data/workspace/sessions mounts
                    if "agents" in dst or "sessions" in dst or "/data" == dst or "openclaw" in dst.lower():
                        log.info(f"  Mount: {src} -> {dst}")
                        if "sessions" in dst:
                            result["sessions_dir"] = src
                        elif "agents" in dst:
                            result["sessions_dir"] = os.path.join(src, "main", "sessions")
                        elif dst in ("/data", "/app", "/home", "/root", "/opt"):
                            # Search mount point for sessions + workspace (up to 3 levels deep)
                            _found_s, _found_w = _find_openclaw_dirs(src)
                            if _found_s:
                                result["sessions_dir"] = _found_s
                            if _found_w:
                                result["workspace"] = _found_w
                    if "workspace" in dst:
                        result["workspace"] = src
                    if "logs" in dst or "tmp" in dst:
                        result["log_dir"] = src
            except Exception as e:
                log.debug(f"Docker inspect error: {e}")
            # If no volume mounts found, try docker exec to find paths
            if not result:
                try:
                    for check_path in ["/root/.openclaw", "/data", "/app"]:
                        chk = subprocess.run(
                            ["docker", "exec", cid, "ls", f"{check_path}/agents/main/sessions"],
                            capture_output=True, text=True, timeout=5)
                        if chk.returncode == 0 and chk.stdout.strip():
                            log.info(f"  Found sessions inside container at {check_path}")
                            # Copy files out to host
                            host_dir = Path.home() / ".clawmetry" / "docker-mirror"
                            host_dir.mkdir(parents=True, exist_ok=True)
                            sessions_mirror = host_dir / "sessions"
                            workspace_mirror = host_dir / "workspace"
                            sessions_mirror.mkdir(exist_ok=True)
                            workspace_mirror.mkdir(exist_ok=True)
                            # rsync from container
                            subprocess.run(["docker", "cp", f"{cid}:{check_path}/agents/main/sessions/.", str(sessions_mirror)],
                                           capture_output=True, timeout=30)
                            subprocess.run(["docker", "cp", f"{cid}:{check_path}/workspace/.", str(workspace_mirror)],
                                           capture_output=True, timeout=30)
                            # Copy logs
                            for log_path in ["/tmp/openclaw", f"{check_path}/logs"]:
                                subprocess.run(["docker", "cp", f"{cid}:{log_path}/.", str(host_dir / "logs")],
                                               capture_output=True, timeout=15)
                            result["sessions_dir"] = str(sessions_mirror)
                            result["workspace"] = str(workspace_mirror)
                            result["log_dir"] = str(host_dir / "logs")
                            result["docker_container"] = cid
                            result["docker_path"] = check_path
                            log.info(f"  Mirrored Docker data to {host_dir}")
                            break
                except Exception as e:
                    log.debug(f"Docker exec fallback error: {e}")
            if result:
                return result
    except FileNotFoundError:
        log.debug("Docker not installed or not in PATH")
    except Exception as e:
        log.debug(f"Docker detection error: {e}")
    return {}


def detect_paths() -> dict:
    home = Path.home()
    # Try Docker detection first (OpenClaw running in container)
    docker_paths = _detect_docker_openclaw()
    if docker_paths.get("sessions_dir"):
        log.info(f"Using Docker-detected paths: {docker_paths}")

    sessions_candidates = [
        home / ".openclaw" / "agents" / "main" / "sessions",
        Path("/data/agents/main/sessions"),
        Path("/app/agents/main/sessions"),
        Path("/root/.openclaw/agents/main/sessions"),
        Path("/opt/openclaw/agents/main/sessions"),
    ]
    oc_home = os.environ.get("OPENCLAW_HOME", "")
    if oc_home:
        sessions_candidates.insert(0, Path(oc_home) / "agents" / "main" / "sessions")
    sessions_dir = docker_paths.get("sessions_dir") or next((str(p) for p in sessions_candidates if p.exists()),
                        str(sessions_candidates[0]))

    log_candidates = [Path("/tmp/openclaw"), home / ".openclaw" / "logs", Path("/data/logs")]
    log_dir = docker_paths.get("log_dir") or next((str(p) for p in log_candidates if p.exists()), "/tmp/openclaw")

    workspace_candidates = [
        home / ".openclaw" / "workspace",
        Path("/data/workspace"),
        Path("/app/workspace"),
    ]
    workspace = docker_paths.get("workspace") or next((str(p) for p in workspace_candidates if p.exists()),
                     str(workspace_candidates[0]))

    log.info(f"Paths: sessions={sessions_dir} logs={log_dir} workspace={workspace}")
    return {"sessions_dir": sessions_dir, "log_dir": log_dir, "workspace": workspace}


# ── Sync: session events (full content, encrypted) ────────────────────────────

def sync_sessions(config: dict, state: dict, paths: dict) -> int:
    sessions_dir = paths["sessions_dir"]
    api_key      = config["api_key"]
    enc_key      = config.get("encryption_key")
    node_id      = config["node_id"]
    last_ids: dict = state.setdefault("last_event_ids", {})
    total = 0

    jsonl_files = sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl")))
    for fpath in jsonl_files:
        fname    = os.path.basename(fpath)
        last_line = last_ids.get(fname, 0)
        batch: list[dict] = []

        try:
            with open(fpath, "r", errors="replace") as f:
                all_lines = f.readlines()

            new_lines = all_lines[last_line:]
            for i, raw in enumerate(new_lines, start=last_line):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue

                # Full content — encrypted before leaving machine
                batch.append(obj)

                if len(batch) >= BATCH_SIZE:
                    _flush_session_batch(batch, fname, api_key, enc_key, node_id)
                    total += len(batch)
                    batch = []

            if batch:
                _flush_session_batch(batch, fname, api_key, enc_key, node_id)
                total += len(batch)

            last_ids[fname] = len(all_lines)

        except Exception as e:
            log.warning(f"Session sync error ({fname}): {e}")

    return total


def _flush_session_batch(batch: list, fname: str, api_key: str,
                          enc_key: str | None, node_id: str) -> None:
    payload = {"session_file": fname, "node_id": node_id, "events": batch}
    if enc_key:
        _post("/ingest/events", {
            "node_id": node_id,
            "encrypted": True,
            "blob": encrypt_payload(payload, enc_key),
        }, api_key)
    else:
        _post("/ingest/events", payload, api_key)


# ── Sync: logs (full lines, encrypted) ────────────────────────────────────────

def sync_logs(config: dict, state: dict, paths: dict) -> int:
    log_dir  = paths["log_dir"]
    api_key  = config["api_key"]
    enc_key  = config.get("encryption_key")
    node_id  = config["node_id"]
    offsets: dict = state.setdefault("last_log_offsets", {})
    total = 0

    log_files = sorted(glob.glob(os.path.join(log_dir, "openclaw-*.log")))[-5:]
    for fpath in log_files:
        fname  = os.path.basename(fpath)
        offset = offsets.get(fname, 0)
        entries: list[dict] = []

        try:
            with open(fpath, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                if offset > size:
                    offset = 0
                f.seek(offset)
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entries.append(json.loads(raw))
                    except Exception:
                        entries.append({"raw": raw})
                    if len(entries) >= BATCH_SIZE:
                        _flush_log_batch(entries, fname, api_key, enc_key, node_id)
                        total += len(entries)
                        entries = []
                offsets[fname] = f.tell()

            if entries:
                _flush_log_batch(entries, fname, api_key, enc_key, node_id)
                total += len(entries)

        except Exception as e:
            log.warning(f"Log sync error ({fname}): {e}")

    return total


def _flush_log_batch(entries: list, fname: str, api_key: str,
                      enc_key: str | None, node_id: str) -> None:
    payload = {"log_file": fname, "node_id": node_id, "lines": entries}
    if enc_key:
        _post("/ingest/logs", {
            "node_id": node_id,
            "encrypted": True,
            "blob": encrypt_payload(payload, enc_key),
        }, api_key)
    else:
        _post("/ingest/logs", payload, api_key)


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def _check_node_commands(config: dict) -> None:
    """Poll Turso for pending commands (update, set_auto_update)."""
    api_key = config.get("api_key", "")
    node_id = config.get("node_id", "")
    if not api_key or not node_id:
        return
    try:
        resp = _post("/api/cloud/nodes/" + node_id + "/commands/pending",
                     {}, api_key, timeout=6)
        for cmd in resp.get("commands", []):
            cmd_id = cmd.get("id", "")
            action = cmd.get("action", "")
            if action == "update":
                import subprocess as _subp
                log.info("[cmd] running self-update")
                r = _subp.run(["pip3", "install", "--upgrade", "--quiet", "clawmetry"],
                               capture_output=True, text=True, timeout=180)
                status = "ok" if r.returncode == 0 else "error"
                detail = (r.stdout + r.stderr).strip()[-300:]
                log.info(f"[cmd] update {status}: {detail}")
                _post("/api/cloud/nodes/" + node_id + "/commands/" + cmd_id + "/ack",
                      {"status": status, "detail": detail}, api_key, timeout=6)
                if status == "ok":
                    import sys, os as _os
                    log.info("[cmd] restarting daemon after update")
                    _os.execv(sys.executable, [sys.executable] + sys.argv)
            elif action == "set_auto_update":
                _val = bool(cmd.get("value", False))
                try:
                    import json as _jx
                    _cfg_data = json.loads(CONFIG_FILE.read_text())
                    _cfg_data["auto_update"] = _val
                    CONFIG_FILE.write_text(json.dumps(_cfg_data, indent=2))
                    config["auto_update"] = _val
                except Exception:
                    pass
                _post("/api/cloud/nodes/" + node_id + "/commands/" + cmd_id + "/ack",
                      {"status": "ok"}, api_key, timeout=6)
            elif action == "set_encryption_key":
                _new_key = cmd.get("value", "")
                if _new_key:
                    try:
                        import json as _jxk
                        _cfg_data = _jxk.loads(CONFIG_FILE.read_text())
                        _cfg_data["encryption_key"] = _new_key
                        CONFIG_FILE.write_text(_jxk.dumps(_cfg_data, indent=2))
                        config["encryption_key"] = _new_key
                        log.info("[cmd] encryption key rotated")
                        _post("/api/cloud/nodes/" + node_id + "/commands/" + cmd_id + "/ack",
                              {"status": "ok"}, api_key, timeout=6)
                    except Exception as _kex:
                        log.warning(f"[cmd] key rotation failed: {_kex}")
                        _post("/api/cloud/nodes/" + node_id + "/commands/" + cmd_id + "/ack",
                              {"status": "error", "detail": str(_kex)}, api_key, timeout=6)
    except Exception:
        pass  # non-critical


def send_heartbeat(config: dict) -> None:
    try:
        _meta = _get_node_metadata()
        _post("/ingest/heartbeat", {
            "node_id": config["node_id"],
            "ts": datetime.now(timezone.utc).isoformat(),
            "platform": platform.system(),
            "version": _get_version(),
            "e2e": bool(config.get("encryption_key")),
            "auto_update": config.get("auto_update", False),
            "node_meta": _meta,
        }, config["api_key"])
    except Exception as e:
        log.debug(f"Heartbeat failed: {e}")
    # Poll for pending commands on every heartbeat
    _check_node_commands(config)


def _get_version() -> str:
    try:
        import re
        src = (Path(__file__).parent.parent / "dashboard.py").read_text(errors="replace")
        m = re.search(r'^__version__\s*=\s*["\'](.+?)["\']', src, re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


# ── Daemon loop ────────────────────────────────────────────────────────────────

def sync_crons(config: dict, state: dict, paths: dict) -> int:
    """Sync cron job definitions to cloud."""
    api_key = config["api_key"]
    node_id = config["node_id"]
    last_hash = state.get("cron_hash", "")

    # Find cron jobs.json
    home = Path.home()
    cron_candidates = [
        home / ".openclaw" / "cron" / "jobs.json",
        home / ".openclaw" / "agents" / "main" / "cron" / "jobs.json",
    ]
    cron_file = next((str(p) for p in cron_candidates if p.exists()), None)
    if not cron_file:
        return 0

    try:
        import hashlib
        raw = open(cron_file, "rb").read()
        h = hashlib.md5(raw).hexdigest()
        if h == last_hash:
            return 0
        data = json.loads(raw)
        jobs = data.get("jobs", []) if isinstance(data, dict) else data

        events = []
        for j in jobs:
            sched = j.get("schedule", {})
            kind = sched.get("kind", "")
            expr = sched.get("interval", "") if kind == "interval" else (
                   f"at {sched.get('at', '')}" if kind == "at" else
                   sched.get("cron", "") if kind == "cron" else "")
            events.append({
                "type": "cron_state", "session_id": "",
                "data": {"job_id": j.get("id",""), "name": j.get("name",""),
                         "enabled": j.get("enabled", True), "expr": expr}
            })

        if events:
            _post("/api/ingest", {"events": events, "node_id": node_id}, api_key)
            state["cron_hash"] = h
            return len(events)
    except Exception as e:
        log.warning(f"Cron sync error: {e}")
    return 0


def sync_session_metadata(config: dict) -> int:
    """Sync OpenClaw session metadata rows to cloud sessions table.
    
    Reads JSONL session files directly (HTTP API returns HTML, not JSON).
    Extracts session_id, model, timestamps from the event stream.
    """
    api_key = config["api_key"]
    node_id = config["node_id"]
    try:
        home = Path.home()
        sessions_candidates = [
            home / ".openclaw" / "agents" / "main" / "sessions",
            Path("/data/agents/main/sessions"),
        ]
        sessions_dir = next((p for p in sessions_candidates if p.exists()), None)
        if not sessions_dir:
            return 0

        session_rows = []
        for fpath in sorted(sessions_dir.glob("*.jsonl"))[-100:]:
            try:
                sid = fpath.stem  # UUID filename = session_id
                model = ""
                started_at = ""
                updated_at = ""
                total_tokens = 0
                total_cost = 0.0
                label = ""

                # Scan session file for metadata, tokens, cost, model
                # Read head for start info, scan all for usage, tail for end
                with open(fpath, "r", errors="replace") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue
                        ts = ev.get("timestamp", "")
                        if not started_at and ts:
                            started_at = ts
                        if ts:
                            updated_at = ts
                        etype = ev.get("type", "")
                        if etype == "model_change" and ev.get("modelId"):
                            model = ev["modelId"]
                        elif etype == "session" and ev.get("label"):
                            label = ev["label"]
                        elif etype == "message":
                            msg = ev.get("message", {})
                            usage = msg.get("usage", {})
                            if usage:
                                total_tokens += int(usage.get("totalTokens", 0))
                                cost_obj = usage.get("cost", {})
                                if isinstance(cost_obj, dict):
                                    total_cost += float(cost_obj.get("total", 0))
                                elif isinstance(cost_obj, (int, float)):
                                    total_cost += float(cost_obj)
                            # Use last model seen in messages
                            msg_model = msg.get("model", "")
                            if msg_model:
                                model = msg_model

                session_rows.append({
                    "session_id": sid,
                    "display_name": label or sid[:8],
                    "status": "completed",
                    "model": model,
                    "total_tokens": total_tokens,
                    "total_cost": total_cost,
                    "started_at": started_at,
                    "updated_at": updated_at,
                })
            except Exception as e:
                log.debug(f"Session parse error ({fpath.name}): {e}")

        if not session_rows:
            return 0

        # Batch in groups of 50
        for i in range(0, len(session_rows), 50):
            batch = session_rows[i:i+50]
            _post("/ingest/sessions", {"node_id": node_id, "sessions": batch}, api_key)
        return len(session_rows)
    except Exception as e:
        log.warning(f"Session metadata sync failed: {e}")
        return 0


def sync_memory(config: dict, state: dict, paths: dict) -> int:
    """Sync memory files (MEMORY.md + memory/*.md) to cloud."""
    workspace = paths.get("workspace", "")
    api_key   = config["api_key"]
    enc_key   = config.get("encryption_key")
    node_id   = config["node_id"]
    import hashlib as _hlm
    _ek_hash = _hlm.sha256(enc_key.encode()).hexdigest()[:16] if enc_key else ""
    if _ek_hash and state.get("_encryption_key_hash","") != _ek_hash:
        # Key changed since state was last written — clear hashes
        state["memory_hashes"] = {}
        state["_encryption_key_hash"] = _ek_hash
    last_hashes: dict = state.setdefault("memory_hashes", {})

    synced = 0

    # Collect all workspace memory files (same list as OSS dashboard)
    memory_files = []
    for name in ['MEMORY.md', 'SOUL.md', 'IDENTITY.md', 'USER.md', 'AGENTS.md', 'TOOLS.md', 'HEARTBEAT.md']:
        fpath = os.path.join(workspace, name)
        if os.path.isfile(fpath):
            memory_files.append((name, fpath))
    mem_dir = os.path.join(workspace, "memory")
    if os.path.isdir(mem_dir):
        for f in sorted(os.listdir(mem_dir)):
            if f.endswith(".md"):
                memory_files.append((f"memory/{f}", os.path.join(mem_dir, f)))

    if not memory_files:
        return 0

    # Check for changes via content hash
    import hashlib
    changed_files = []
    file_list = []
    for name, path in memory_files:
        try:
            content_bytes = open(path, "rb").read()
            h = hashlib.md5(content_bytes).hexdigest()
            file_list.append({"name": name, "size": len(content_bytes), "modified": os.path.getmtime(path)})
            if h != last_hashes.get(name):
                changed_files.append((name, content_bytes.decode("utf-8", errors="replace")))
                last_hashes[name] = h
        except Exception as e:
            log.debug(f"Memory file read error ({name}): {e}")

    if not changed_files:
        return 0

    # Push memory files as encrypted blob (like session events)
    payload = {
        "node_id": node_id,
        "memory_state": {"files": file_list},
        "memory_content": [{"path": name, "content": content[:100000]} for name, content in changed_files],
    }
    try:
        if enc_key:
            from clawmetry.sync import encrypt_payload
            _post("/ingest/memory", {
                "node_id": node_id,
                "encrypted": True,
                "blob": encrypt_payload(payload, enc_key),
            }, api_key)
        else:
            _post("/ingest/memory", payload, api_key)
        synced = len(changed_files)
    except Exception as e:
        log.warning(f"Memory sync error: {e}")

    return synced



# ── Real-time log streaming ────────────────────────────────────────────────────



    """Build memory file list for the Memory popup."""



def _build_machine_info():
    """Build machine hardware info for the Machine popup."""
    try:
        import platform, subprocess, socket
        items = []
        items.append({"label": "Hostname", "value": socket.gethostname(), "status": "ok"})
        # IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            items.append({"label": "IP", "value": ip, "status": "ok"})
        except Exception:
            items.append({"label": "IP", "value": "unknown", "status": "warning"})
        # CPU
        items.append({"label": "CPU", "value": platform.machine(), "status": "ok"})
        # CPU Cores
        try:
            import multiprocessing
            items.append({"label": "CPU Cores", "value": str(multiprocessing.cpu_count()), "status": "ok"})
        except Exception:
            pass
        # Load average
        try:
            load = os.getloadavg()
            items.append({"label": "Load (1/5/15m)", "value": f"{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}", "status": "ok"})
        except Exception:
            pass
        # GPU
        try:
            gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=5).decode().strip()
            items.append({"label": "GPU", "value": gpu, "status": "ok"})
        except Exception:
            items.append({"label": "GPU", "value": "N/A (no nvidia-smi)", "status": "ok"})
        # Kernel
        items.append({"label": "Kernel", "value": platform.release(), "status": "ok"})
        return {"items": items}
    except Exception as e:
        log.warning(f"Machine info error: {e}")
        return {"items": []}

def _build_runtime_info():
    """Build runtime environment info for the Runtime popup."""
    try:
        import platform, subprocess
        items = []
        items.append({"label": "Python", "value": platform.python_version(), "status": "ok"})
        items.append({"label": "OS", "value": f"{platform.system()} {platform.release()}", "status": "ok"})
        items.append({"label": "Architecture", "value": platform.machine(), "status": "ok"})
        # OpenClaw version
        try:
            oc_ver = subprocess.check_output(["openclaw", "--version"], stderr=subprocess.STDOUT, timeout=5).decode().strip()
            items.append({"label": "OpenClaw", "value": oc_ver, "status": "ok"})
        except Exception:
            items.append({"label": "OpenClaw", "value": "unknown", "status": "warning"})
        # Disk /
        try:
            df = subprocess.check_output(["df", "-h", "/"], timeout=5).decode().strip().split("\n")
            if len(df) >= 2:
                parts = df[1].split()
                pct = int(parts[4].replace("%", ""))
                st = "critical" if pct > 90 else "warning" if pct > 80 else "ok"
                items.append({"label": "Disk /", "value": f"{parts[2]} / {parts[1]} ({parts[4]} used)", "status": st})
        except Exception:
            pass
        # Node.js
        try:
            nv = subprocess.check_output(["node", "--version"], timeout=5).decode().strip()
            items.append({"label": "Node.js", "value": nv, "status": "ok"})
        except Exception:
            pass
        return {"items": items}
    except Exception as e:
        log.warning(f"Runtime info error: {e}")
        return {"items": []}

def _build_memory_files(workspace):
    """Build memory file list for the Memory popup."""
    if not workspace or not os.path.isdir(workspace):
        return []
    files = []
    for name in ["MEMORY.md", "SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md"]:
        fpath = os.path.join(workspace, name)
        if os.path.isfile(fpath):
            try:
                st = os.stat(fpath)
                files.append({"name": name, "size": st.st_size, "modified": st.st_mtime})
            except Exception:
                pass
    mem_dir = os.path.join(workspace, "memory")
    if os.path.isdir(mem_dir):
        for f in sorted(os.listdir(mem_dir)):
            if f.endswith(".md"):
                fpath = os.path.join(mem_dir, f)
                try:
                    st = os.stat(fpath)
                    files.append({"name": f"memory/{f}", "size": st.st_size, "modified": st.st_mtime})
                except Exception:
                    pass
    return files

def _build_brain_data():
    """Build LLM call data for the Brain/AI Model popup."""
    try:
        import collections
        home = str(Path.home())
        session_dir = os.path.join(home, ".openclaw", "agents", "main", "sessions")
        if not os.path.isdir(session_dir):
            return {"stats": {}, "calls": []}

        calls = []
        total_cost = 0.0
        total_tokens_in = 0
        total_tokens_out = 0
        total_cache_read = 0
        total_cache_write = 0
        total_duration = 0
        thinking_calls = 0
        cache_hit_calls = 0
        model_name = "unknown"

        today = datetime.now().strftime("%Y-%m-%d")

        files = sorted(glob.glob(os.path.join(session_dir, "*.jsonl")), key=os.path.getmtime, reverse=True)[:20]

        for fp in files:
            try:
                session_name = os.path.basename(fp).split(".")[0][:12]
                for line_raw in open(fp, errors="ignore"):
                    try:
                        ev = json.loads(line_raw)
                        if ev.get("type") != "message":
                            continue
                        msg = ev.get("message", {})
                        role = msg.get("role", "")
                        if role != "assistant":
                            continue

                        usage = msg.get("usage") or ev.get("usage") or {}
                        if not usage:
                            continue

                        ts = ev.get("timestamp", "")
                        if not ts or today not in ts[:10]:
                            continue

                        tok_in = usage.get("inputTokens", 0) or usage.get("input_tokens", 0) or 0
                        tok_out = usage.get("outputTokens", 0) or usage.get("output_tokens", 0) or 0
                        cr = usage.get("cacheReadInputTokens", 0) or usage.get("cache_read_input_tokens", 0) or 0
                        cw = usage.get("cacheCreationInputTokens", 0) or usage.get("cache_creation_input_tokens", 0) or 0

                        cost = (tok_in * 15 + tok_out * 75 + cr * 1.5 + cw * 18.75) / 1_000_000
                        dur_ms = int(ev.get("durationMs", 0) or ev.get("duration_ms", 0) or 0)

                        has_thinking = False
                        tools_used = []
                        if isinstance(msg.get("content"), list):
                            for c in msg["content"]:
                                if c.get("type") == "thinking":
                                    has_thinking = True
                                elif c.get("type") == "toolCall":
                                    tn = c.get("name", "")
                                    if tn and tn not in tools_used:
                                        tools_used.append(tn)

                        m = msg.get("model") or ev.get("model") or ""
                        if m and m != "unknown":
                            model_name = m.split("/")[-1] if "/" in m else m

                        total_tokens_in += tok_in
                        total_tokens_out += tok_out
                        total_cache_read += cr
                        total_cache_write += cw
                        total_cost += cost
                        total_duration += dur_ms
                        if has_thinking:
                            thinking_calls += 1
                        if cr > 0:
                            cache_hit_calls += 1

                        calls.append({
                            "timestamp": ts,
                            "session": session_name,
                            "tokens_in": tok_in,
                            "tokens_out": tok_out,
                            "cost": "$" + format(cost, ".4f"),
                            "duration_ms": dur_ms,
                            "thinking": has_thinking,
                            "cache_read": cr,
                            "tools_used": tools_used[:5],
                        })
                    except Exception:
                        continue
            except Exception:
                continue

        calls.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        calls = calls[:100]

        n = len(calls)
        avg_ms = int(total_duration / n) if n > 0 else 0

        stats = {
            "model": model_name,
            "today_calls": n,
            "today_cost": "$" + format(total_cost, ".2f"),
            "avg_response_ms": avg_ms,
            "thinking_calls": thinking_calls,
            "cache_hits": cache_hit_calls,
            "today_tokens": {
                "input": total_tokens_in,
                "output": total_tokens_out,
                "cache_read": total_cache_read,
                "cache_write": total_cache_write,
            },
        }

        return {"stats": stats, "calls": calls, "total": n}
    except Exception as e:
        log.warning(f"Brain data error: {e}")
        return {"stats": {}, "calls": [], "total": 0}

def _build_tool_stats():
    """Build tool usage stats from recent session logs."""
    try:
        import collections, glob
        home = str(Path.home())
        session_dir = os.path.join(home, ".openclaw", "agents", "main", "sessions")
        if not os.path.isdir(session_dir):
            return {}
        
        tool_counts = collections.Counter()
        tool_recent = {}  # tool_name -> last few entries
        channel_msgs = collections.defaultdict(lambda: {"in": 0, "out": 0, "messages": []})
        
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Read last 20 active sessions
        files = sorted(glob.glob(os.path.join(session_dir, "*.jsonl")), key=os.path.getmtime, reverse=True)[:20]
        
        for fp in files:
            try:
                for line in open(fp, errors="ignore"):
                    try:
                        ev = json.loads(line)
                        if ev.get("type") != "message":
                            continue
                        msg = ev.get("message", {})
                        ts = ev.get("timestamp", "")
                        role = msg.get("role", "")
                        
                        if isinstance(msg.get("content"), list):
                            for c in msg["content"]:
                                if c.get("type") == "toolCall":
                                    name = c.get("name", "?")
                                    tool_counts[name] += 1
                                    args = c.get("arguments", {}) or c.get("input", {}) or c.get("args", {}) or {}
                                    if isinstance(args, str):
                                        try: args = json.loads(args)
                                        except: args = {}
                                    
                                    # Track recent entries for specific tools
                                    if name == "web_search":
                                        q = args.get("query", "")
                                        if q and name not in tool_recent:
                                            tool_recent[name] = []
                                        if q:
                                            tool_recent.setdefault(name, []).append({"query": q[:200], "ts": ts})
                                    elif name == "web_fetch":
                                        url = args.get("url", "")
                                        if url:
                                            tool_recent.setdefault(name, []).append({"url": url[:200], "ts": ts})
                                    elif name == "browser":
                                        action = args.get("action", "")
                                        url = args.get("url", "")
                                        tool_recent.setdefault(name, []).append({"action": action, "url": url[:200] if url else "", "ts": ts})
                                    elif name == "exec":
                                        cmd = args.get("command", "")
                                        if cmd:
                                            tool_recent.setdefault(name, []).append({"command": cmd[:300], "ts": ts})
                                    elif name == "message":
                                        target = args.get("target", "") or args.get("channel", "")
                                        tool_recent.setdefault(name, []).append({"target": target, "ts": ts})
                        
                        # Track channel messages from inbound context
                        if role == "user":
                            text = ""
                            if isinstance(msg.get("content"), str):
                                text = msg["content"][:300]
                            elif isinstance(msg.get("content"), list):
                                for c in msg["content"]:
                                    if c.get("type") == "text":
                                        text = c.get("text", "")[:300]
                                        break
                            
                            # Try to detect channel from metadata
                            meta = ev.get("metadata", {}) or {}
                            channel = meta.get("channel", "") or meta.get("surface", "")
                            if channel and text:
                                channel_msgs[channel]["in"] += 1
                                channel_msgs[channel]["messages"].append({
                                    "direction": "in", "content": text[:200],
                                    "timestamp": ts, "sender": meta.get("sender", "User")
                                })
                    except Exception:
                        continue
            except Exception:
                continue
        
        # Cap recent entries
        for name in tool_recent:
            tool_recent[name] = tool_recent[name][-30:]
            tool_recent[name].reverse()
        
        for ch in channel_msgs:
            channel_msgs[ch]["messages"] = channel_msgs[ch]["messages"][-30:]
            channel_msgs[ch]["messages"].reverse()
        
        return {
            "counts": dict(tool_counts.most_common(30)),
            "recent": {k: v for k, v in tool_recent.items()},
            "channelMsgs": dict(channel_msgs),
        }
    except Exception as e:
        log.warning(f"Tool stats error: {e}")
        return {}

def _build_channel_list(config):
    """Build list of configured channels."""
    try:
        home = str(Path.home())
        oc_config = os.path.join(home, ".openclaw", "openclaw.json")
        if not os.path.isfile(oc_config):
            return []
        data = json.load(open(oc_config))
        channels = []
        ch_section = data.get("channels", {})
        if isinstance(ch_section, dict):
            for key in ch_section:
                channels.append({"name": key, "enabled": True})
        # Also check top-level channel keys
        for key in ("telegram", "discord", "slack", "whatsapp", "signal", "irc", "webchat", "imessage"):
            if key in data and key not in [c["name"] for c in channels]:
                cfg = data[key]
                if isinstance(cfg, dict):
                    channels.append({"name": key, "enabled": cfg.get("enabled", True)})
        return channels
    except Exception:
        return []



def _build_channel_data(config):
    """Build recent channel message data from OpenClaw log files."""
    import re as _re
    try:
        home = str(Path.home())
        log_dir = "/tmp/openclaw" if sys.platform != "win32" else os.path.join(home, ".openclaw", "logs")
        if not os.path.isdir(log_dir):
            return {}
        
        # Find recent log files
        log_files = sorted(glob.glob(os.path.join(log_dir, "*.log")), reverse=True)[:2]
        if not log_files:
            return {}
        
        channels = {}
        today = datetime.now().strftime("%Y-%m-%d")
        
        for lf in log_files:
            try:
                with open(lf, 'r', errors='ignore') as f:
                    for line in f:
                        if 'messageChannel=' not in line and 'telegram' not in line.lower():
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        msg1 = obj.get("1", "") or obj.get("0", "")
                        ts = obj.get("time", "") or ""
                        
                        # Extract channel name
                        ch_match = _re.search(r'messageChannel=(\w+)', msg1)
                        if not ch_match:
                            continue
                        ch_name = ch_match.group(1).lower()
                        
                        if ch_name not in channels:
                            channels[ch_name] = {"messages": [], "todayIn": 0, "todayOut": 0, "total": 0}
                        
                        # Determine direction
                        direction = "in" if "run start" in msg1 else "out"
                        if "deliver" in msg1.lower():
                            direction = "out"
                        
                        # Extract content
                        text = ""
                        content_match = _re.search(r'content=(.{1,200}?)(?:\s+\w+=|$)', msg1)
                        if content_match:
                            text = content_match.group(1)[:200]
                        
                        channels[ch_name]["messages"].append({
                            "direction": direction,
                            "content": text,
                            "timestamp": ts,
                            "sender": "User" if direction == "in" else "Clawd",
                        })
                        channels[ch_name]["total"] += 1
                        if today in ts:
                            if direction == "in":
                                channels[ch_name]["todayIn"] += 1
                            else:
                                channels[ch_name]["todayOut"] += 1
            except Exception:
                continue
        
        # Cap and reverse
        for ch in channels.values():
            ch["messages"] = ch["messages"][-30:]
            ch["messages"].reverse()
        
        return channels
    except Exception as e:
        log.warning(f"Channel data error: {e}")
        return {}




def _correlate_cron_sessions(last_run_at_ms, session_dir, tolerance_s=90, max_runs=10):
    """Find sessions that match cron run times and extract token/cost data."""
    import glob as _g
    results = []
    if not last_run_at_ms or not os.path.isdir(session_dir):
        return results
    target_ts = last_run_at_ms / 1000.0
    candidates = []
    try:
        for fpath in _g.glob(os.path.join(session_dir, "*.jsonl")):
            try:
                with open(fpath, errors="ignore") as f:
                    first = f.readline()
                if not first.strip():
                    continue
                ev = json.loads(first)
                ts_str = ev.get("timestamp", "")
                if not ts_str:
                    continue
                from datetime import datetime, timezone as _tz
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                file_ts = dt.timestamp()
                delta = abs(file_ts - target_ts)
                candidates.append((delta, file_ts, fpath))
            except Exception:
                continue
    except Exception:
        return results
    candidates.sort(key=lambda x: x[0])
    for delta, file_ts, fpath in candidates[:1]:
        if delta > tolerance_s:
            break
        total_in = 0
        total_out = 0
        cost_usd = None
        model = None
        try:
            with open(fpath, errors="ignore") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                        if ev.get("type") == "summary":
                            msg = ev.get("message", {})
                            if msg.get("totalTokens"):
                                total_in = msg["totalTokens"]
                            if msg.get("costUsd"):
                                cost_usd = float(msg["costUsd"])
                        usage = ev.get("usage") or (ev.get("message") or {}).get("usage") or {}
                        if usage:
                            total_in += int(usage.get("input_tokens", 0) or 0)
                            total_out += int(usage.get("output_tokens", 0) or 0)
                        msg = ev.get("message", {})
                        if msg.get("model") and not model:
                            model = msg["model"]
                    except Exception:
                        continue
        except Exception:
            pass
        total_tokens = total_in + total_out
        results.append({
            "ts": file_ts * 1000,
            "tokens": total_tokens or None,
            "costUsd": cost_usd,
            "model": model,
            "sessionFile": os.path.basename(fpath),
        })
    return results

def _build_cron_jobs(paths):
    """Build cron jobs list for snapshot."""
    import json as _j2
    home = str(Path.home())
    cron_candidates = [
        os.path.join(home, ".openclaw", "cron", "jobs.json"),
        os.path.join(home, ".openclaw", "agents", "main", "cron", "jobs.json"),
    ]
    cron_file = next((p for p in cron_candidates if os.path.isfile(p)), None)
    if not cron_file:
        return []
    try:
        data = _j2.load(open(cron_file))
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        result = []
        for j in jobs:
            sched = j.get("schedule", {})
            kind = sched.get("kind", "")
            expr = sched.get("interval", "") if kind == "interval" else (
                   f"at {sched.get('at', '')}" if kind == "at" else
                   sched.get("cron", "") if kind == "cron" else "")
            sched_obj = j.get("schedule", {})
            state = j.get("state", {})
            last_run_ms = state.get("lastRunAtMs")
            session_dir = os.path.join(str(Path.home()), ".openclaw", "agents", "main", "sessions")
            cost_info = _correlate_cron_sessions(last_run_ms, session_dir)
            last_tokens = cost_info[0]["tokens"] if cost_info else None
            last_cost = cost_info[0]["costUsd"] if cost_info else None
            last_model = cost_info[0]["model"] if cost_info else None
            result.append({
                "id": j.get("id", ""),
                "name": j.get("name", ""),
                "enabled": j.get("enabled", True),
                "schedule": sched_obj,
                "task": j.get("task", "")[:200],
                "state": state,
                "lastRun": None,
                "lastStatus": None,
                "lastRunTokens": last_tokens,
                "lastRunCostUsd": last_cost,
                "lastRunModel": last_model,
                "runHistory": cost_info,
            })
        return result
    except Exception:
        return []

def sync_system_snapshot(config: dict, state: dict, paths: dict) -> int:
    """Push system info + subagent data as encrypted snapshot."""
    import subprocess, platform, json as _json
    api_key = config["api_key"]
    enc_key = config.get("encryption_key")
    node_id = config["node_id"]
    if not enc_key:
        return 0

    # System info
    system = []
    try:
        disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5).stdout.strip().split("\n")[-1].split()
        disk_pct = int(disk[4].replace("%", "")) if len(disk) > 4 else 0
        disk_color = "green" if disk_pct < 80 else ("yellow" if disk_pct < 90 else "red")
        system.append(["Disk /", f"{disk[2]} / {disk[1]} ({disk[4]})", disk_color])
    except Exception:
        system.append(["Disk /", "--", ""])
    # Check for additional data drives
    for extra_mount in ["/mnt/data-drive", "/data", "/mnt/data", "/home"]:
        try:
            ed = subprocess.run(["df", "-h", extra_mount], capture_output=True, text=True, timeout=3).stdout.strip().split("\n")[-1].split()
            if len(ed) > 4 and ed[5] != "/":
                ep = int(ed[4].replace("%", ""))
                ec = "green" if ep < 80 else ("yellow" if ep < 90 else "red")
                system.append([f"Disk {ed[5]}", f"{ed[2]} / {ed[1]} ({ed[4]})", ec])
        except Exception:
            pass

    try:
        if sys.platform == "darwin":
            import re as _re
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            pages = {m.group(1): int(m.group(2)) for m in _re.finditer(r'"(.+?)"\s*:\s*(\d+)', vm)}
            page_size = 16384
            used = (pages.get("Pages active", 0) + pages.get("Pages wired down", 0)) * page_size
            total_raw = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip()
            total = int(total_raw) if total_raw else 0
            system.append(["RAM", f"{used // (1024**3)}G / {total // (1024**3)}G", ""])
        else:
            mem = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5).stdout.strip().split("\n")[1].split()
            system.append(["RAM", f"{mem[2]} / {mem[1]}", ""])
    except Exception:
        system.append(["RAM", "--", ""])

    try:
        if sys.platform == "darwin":
            up = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5).stdout.strip()
            system.append(["Uptime", up.split(",")[0].split("up")[-1].strip(), ""])
        else:
            up = subprocess.run(["uptime", "-p"], capture_output=True, text=True, timeout=5).stdout.strip()
            system.append(["Uptime", up.replace("up ", ""), ""])
    except Exception:
        system.append(["Uptime", "--", ""])

    # Gateway status
    try:
        gw = subprocess.run(["pgrep", "-f", "openclaw"], capture_output=True, text=True, timeout=5)
        gw_running = gw.returncode == 0
        system.append(["Gateway", "Running" if gw_running else "Stopped", "green" if gw_running else "red"])
    except Exception:
        system.append(["Gateway", "--", ""])

    # Infra
    uname = platform.uname()
    infra = {
        "machine": uname.node,
        "runtime": f"Node.js - {uname.system} {uname.release.split('-')[0]}",
        "storage": system[0][1] if system else "--",
    }

    # Session info
    sessions_dir = paths.get("sessions_dir", "")
    session_count = 0
    model_name = ""
    main_tokens = 0
    subagents_list = []
    active_count = 0

    index_path = os.path.join(sessions_dir, "sessions.json") if sessions_dir else ""
    if index_path and os.path.isfile(index_path):
        try:
            with open(index_path) as f:
                index = _json.load(f)
            now_ms = time.time() * 1000
            for key, meta in index.items():
                if not isinstance(meta, dict):
                    continue
                session_count += 1
                if ":subagent:" in key:
                    age_ms = now_ms - meta.get("updatedAt", 0)
                    status = "active" if age_ms < 120000 else ("idle" if age_ms < 3600000 else "stale")
                    if status == "active":
                        active_count += 1
                    subagents_list.append({
                        "label": meta.get("label", key.split(":")[-1][:12]),
                        "status": status,
                        "model": meta.get("model", ""),
                        "task": meta.get("task", "")[:100],
                        "tokens": meta.get("totalTokens", 0),
                        "sessionId": key.split(":")[-1],
                        "key": key,
                        "displayName": meta.get("label", meta.get("task", key.split(":")[-1][:12]))[:80],
                        "updatedAt": meta.get("updatedAt", 0),
                        "runtimeMs": int(now_ms - meta.get("createdAt", meta.get("updatedAt", now_ms))),
                    })
                elif "subagent" not in key:
                    if not model_name:
                        model_name = meta.get("model", "")
                    main_tokens = max(main_tokens, meta.get("totalTokens", 0))
        except Exception as e:
            log.debug(f"Session index read error: {e}")

    # Crons
    cron_enabled = 0
    cron_disabled = 0
    try:
        home = os.path.expanduser("~")
        cron_candidates = [
            os.path.join(home, ".openclaw", "cron", "jobs.json"),
            os.path.join(home, ".openclaw", "agents", "main", "cron", "jobs.json"),
            os.path.join(paths.get("workspace", ""), "..", "crons.json"),
        ]
        cron_path = next((p for p in cron_candidates if os.path.isfile(p)), None)
        if cron_path:
            cron_data = _json.load(open(cron_path))
            crons = cron_data.get("jobs", cron_data) if isinstance(cron_data, dict) else cron_data
            if isinstance(crons, list):
                for c in crons:
                    if c.get("enabled", True):
                        cron_enabled += 1
                    else:
                        cron_disabled += 1
    except Exception:
        pass

    # Memory files
    _mem_files = _build_memory_files(paths.get("workspace", ""))

    # Spending (from state if available)
    spending = state.get("spending", {"today": 0, "week": 0, "month": 0})

    payload = {
        "system": system,
        "infra": infra,
        "model": model_name or "unknown",
        "provider": "",
        "sessionCount": session_count,
        "mainTokens": main_tokens,
        "contextWindow": 200000,
        "cronCount": cron_enabled + cron_disabled,
        "cronEnabled": cron_enabled,
        "cronDisabled": cron_disabled,
        "memoryCount": len(_mem_files),
        "memorySize": sum(f.get("size", 0) for f in _mem_files),
        "memoryFiles": _mem_files,
        "subagents": subagents_list,
        "subagentCounts": {"active": active_count, "idle": len([s for s in subagents_list if s["status"] == "idle"]),
                           "stale": len([s for s in subagents_list if s["status"] == "stale"]), "total": len(subagents_list)},
        "totalActive": active_count,
        "spending": spending,
        "cronJobs": _build_cron_jobs(paths),
        "channels": _build_channel_data(config),
        "toolStats": _build_tool_stats(),
        "brainData": _build_brain_data(),
        "runtimeInfo": _build_runtime_info(),
        "machineInfo": _build_machine_info(),
        "channelList": _build_channel_list(config),
    }

    log.info(f"System snapshot: {len(subagents_list)} subagents ({active_count} active)")

    try:
        _post("/ingest/system-snapshot", {
            "node_id": node_id,
            "encrypted": True,
            "blob": encrypt_payload(payload, enc_key),
        }, api_key)
        return 1
    except Exception as e:
        log.warning(f"System snapshot sync error: {e}")
        return 0


# ── Real-time log streaming ────────────────────────────────────────────────────

def start_log_streamer(config: dict, paths: dict) -> threading.Thread:
    """Start a background thread that tails the local log file and POSTs lines to cloud in real-time."""


def start_log_streamer(config: dict, paths: dict) -> threading.Thread:
    """Start a background thread that tails the local log file and POSTs lines to cloud in real-time."""
    api_key = config["api_key"]
    node_id = config["node_id"]
    log_dir = paths.get("log_dir", "")

    def _find_latest_log():
        if not log_dir or not os.path.isdir(log_dir):
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        candidates = sorted(glob.glob(os.path.join(log_dir, f"*{today}*")), reverse=True)
        if candidates:
            return candidates[0]
        # Fallback: most recent log file
        all_logs = sorted(glob.glob(os.path.join(log_dir, "*.log")), key=os.path.getmtime, reverse=True)
        return all_logs[0] if all_logs else None

    def _stream_worker():
        log.info(f"Log streamer started — watching {log_dir}")
        current_file = None
        proc = None
        batch = []
        last_push = time.time()

        while True:
            try:
                # Find/rotate to latest log file
                latest = _find_latest_log()
                if latest != current_file:
                    if proc:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    current_file = latest
                    if not current_file:
                        time.sleep(5)
                        continue
                    proc = subprocess.Popen(
                        ["tail", "-f", "-n", "0", current_file],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )
                    log.info(f"Tailing {current_file}")

                if not proc or not proc.stdout:
                    time.sleep(2)
                    continue

                # Non-blocking read with select
                import select
                ready, _, _ = select.select([proc.stdout], [], [], STREAM_INTERVAL)
                if ready:
                    line = proc.stdout.readline()
                    if line:
                        batch.append(line.rstrip())

                # Push batch every STREAM_INTERVAL seconds
                now = time.time()
                if batch and (now - last_push >= STREAM_INTERVAL or len(batch) >= 50):
                    try:
                        _post("/ingest/stream", {"node_id": node_id, "lines": batch}, api_key)
                    except Exception as e:
                        log.debug(f"Stream push error: {e}")
                    batch = []
                    last_push = now

            except Exception as e:
                log.debug(f"Stream worker error: {e}")
                time.sleep(5)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc = None
                    current_file = None

    t = threading.Thread(target=_stream_worker, daemon=True, name="log-streamer")
    t.start()
    return t


def run_daemon() -> None:
    config = load_config()
    # If node_id looks like email prefix (contains + or @), use hostname instead
    nid = config.get("node_id", "")
    if not nid:
        import socket
        config["node_id"] = socket.gethostname() or platform.node() or "unknown"
        save_config(config)
        log.info(f"Auto-set node_id:  → {config['node_id']!r}")
    paths  = detect_paths()
    enc    = "🔒 E2E encrypted" if config.get("encryption_key") else "⚠️  unencrypted"
    log.info(f"Starting sync daemon — node={config['node_id']} → {INGEST_URL} ({enc})")

    # ── First-run: full synchronous sync so customer sees data immediately ──
    send_heartbeat(config)
    log.info("Initial heartbeat sent")

    # ── Key rotation detection ───────────────────────────────────────────────
    current_key = config.get("encryption_key", "")
    _state_check = load_state()
    last_key = _state_check.get("_encryption_key_hash", "")
    import hashlib as _hl
    current_key_hash = _hl.sha256(current_key.encode()).hexdigest()[:16] if current_key else ""
    if current_key_hash and last_key and current_key_hash != last_key:
        log.info("Encryption key rotation detected — clearing memory hash cache so files re-upload with new key")
        _state_check["memory_hashes"] = {}
        _state_check["_encryption_key_hash"] = current_key_hash
        save_state(_state_check)
    elif current_key_hash and not last_key:
        _state_check["_encryption_key_hash"] = current_key_hash
        save_state(_state_check)

    # ── Multiple instance detection ──────────────────────────────────────────
    import os as _os
    _pid_file = CONFIG_DIR / "sync.pid"
    _my_pid = str(_os.getpid())
    if _pid_file.exists():
        _old_pid = _pid_file.read_text().strip()
        if _old_pid != _my_pid:
            try:
                _os.kill(int(_old_pid), 0)   # 0 = just check, no signal
                log.warning(f"Another sync daemon is already running (PID {_old_pid}). "
                            f"Multiple instances can cause key conflicts. "
                            f"Stop the old instance with: kill {_old_pid}")
            except (ProcessLookupError, ValueError):
                pass   # old PID is gone, safe to continue
    _pid_file.write_text(_my_pid)

    # First run: either no state file, OR the account changed (new api_key)
    _state_pre = load_state() if STATE_FILE.exists() else {}
    import hashlib as _hr2
    _cur_key_id = _hr2.sha256(config.get("api_key","").encode()).hexdigest()[:16]
    _prev_key_id = _state_pre.get("_api_key_id","")
    first_run = not STATE_FILE.exists() or (_cur_key_id and _prev_key_id and _cur_key_id != _prev_key_id)
    if first_run:
        if not STATE_FILE.exists():
            log.info("First run detected — performing full initial sync...")
        else:
            log.info("New account detected — performing full initial sync...")
        # Save new api_key_id so next connect with same account skips re-sync
        _state_pre["_api_key_id"] = _cur_key_id
        save_state(_state_pre)
        state = load_state()
        try:
            mem = sync_memory(config, state, paths)
            log.info(f"  Memory: {mem} files synced")
        except Exception as e:
            log.warning(f"  Memory sync error: {e}")
        try:
            ev = sync_sessions(config, state, paths)
            log.info(f"  Sessions: {ev} events synced")
        except Exception as e:
            log.warning(f"  Session sync error: {e}")
        try:
            sm = sync_session_metadata(config)
            log.info(f"  Session metadata: {sm} rows synced")
        except Exception as e:
            log.warning(f"  Session metadata error: {e}")
        try:
            lg = sync_logs(config, state, paths)
            log.info(f"  Logs: {lg} lines synced")
        except Exception as e:
            log.warning(f"  Log sync error: {e}")
        try:
            cr = sync_crons(config, state, paths)
            log.info(f"  Crons: {cr} synced")
        except Exception as e:
            log.warning(f"  Cron sync error: {e}")
        state["last_sync"] = datetime.now(timezone.utc).isoformat()
        state["_api_key_id"] = _cur_key_id
        save_state(state)
        send_heartbeat(config)
        log.info("Initial sync complete — node fully visible in cloud")

    # Always sync session metadata on connect (fast, ensures Model/Sessions show correctly)
    if not first_run:
        try:
            sm2 = sync_session_metadata(config)
            log.info(f"Session metadata refreshed: {sm2} rows")
        except Exception as e:
            log.warning(f"Session metadata refresh error: {e}")

    # Start real-time log streamer in background
    start_log_streamer(config, paths)

    heartbeat_interval = 60
    last_heartbeat = time.time()

    while True:
        try:
            state = load_state()
            ev = sync_sessions(config, state, paths)
            lg = sync_logs(config, state, paths)
            mem = sync_memory(config, state, paths)
            crons = sync_crons(config, state, paths)
            sm = sync_session_metadata(config)
            snap = sync_system_snapshot(config, state, paths)
            state["last_sync"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            if ev or lg or mem or crons or sm:
                log.info(f"Synced {ev} events, {lg} log lines, {mem} memory files, {crons} crons, {sm} session rows ({enc})")

            # Re-mirror Docker data if running in Docker mode
            if hasattr(detect_paths, "_docker_cid") or any("docker-mirror" in str(v) for v in paths.values()):
                try:
                    fresh = _detect_docker_openclaw()
                    if fresh.get("sessions_dir"):
                        paths.update({k: v for k, v in fresh.items() if k in paths})
                except Exception:
                    pass

            now = time.time()
            if now - last_heartbeat > heartbeat_interval:
                send_heartbeat(config)
                last_heartbeat = now

        except Exception as e:
            log.error(f"Sync cycle error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    while True:
        try:
            run_daemon()
            break  # clean exit
        except KeyboardInterrupt:
            break
        except Exception as e:
            import traceback
            log.error(f"Daemon crashed: {e}")
            log.error(traceback.format_exc())
            log.info("Restarting in 15 seconds...")
            time.sleep(15)
