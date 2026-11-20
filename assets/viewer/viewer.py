#!/usr/bin/env python3
"""Serve a fixed, trusted local CLI and keep its run records."""

import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import quote, unquote, urlsplit
import uuid

FIELD_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")
RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
FILE_LIMIT = 4 * 1024 * 1024
REQUEST_LIMIT = 24 * 1024 * 1024
LOG_LIMIT = 32 * 1024
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
TEMPLATE = Path(__file__).resolve().parent


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, content):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def validate_filename(name):
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or len(name.encode("utf-8", errors="replace")) > 240
        or any(ord(char) < 32 or char in "/\\" or 0xD800 <= ord(char) <= 0xDFFF for char in name)
    ):
        raise RequestError("文件名不支持，请重命名后重试。")
    return name


def validate_scalar(field, value):
    kind = field["type"]
    valid = False
    if kind == "boolean":
        valid = type(value) is bool
    if kind == "text":
        valid = isinstance(value, str) and len(value) <= 4000 and "\0" not in value
        if valid:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                valid = False
    if kind == "select":
        valid = isinstance(value, str) and value in field["options"]
    if kind in {"integer", "number"}:
        valid = type(value) in {int, float} and abs(value) <= 2**53 - 1 and math.isfinite(value)
        if valid and kind == "integer":
            valid = int(value) == value
        if valid:
            valid = field.get("min", -math.inf) <= value <= field.get("max", math.inf)
    if not valid:
        raise RequestError(f"请检查参数「{field['label']}」的类型或范围。")
    if kind == "integer":
        return int(value)
    return value


def resolve_output(run, name="."):
    output = run / "outputs"
    if output.is_symlink():
        raise RequestError("文件不存在。", 404)
    path = (output / name).resolve()
    if not path.is_relative_to(output.resolve()):
        raise RequestError("文件不存在。", 404)
    return path


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Runner:
    def __init__(self, manifest_path):
        self.path = manifest_path.resolve()
        self.root = self.path.parent
        self.manifest_hash = sha_file(self.path)
        self.config = read_json(self.path)
        self.cwd = (self.root / self.config.get("cwd", ".")).resolve()
        if not self.cwd.is_dir():
            raise RequestError("工作目录不存在。")
        command = self.config["command"]
        if not command or not isinstance(command[0], str) or not command[0]:
            raise RequestError("command 必须以固定的可执行文件开头。")
        timeout = self.config.get("timeout_seconds", 120)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise RequestError("timeout_seconds 必须为 1–3600 的整数。")
        self.timeout = timeout
        self.fields = {}
        for field in self.config.get("fields", []):
            key, kind = field["id"], field["type"]
            if not FIELD_ID.fullmatch(key) or key in self.fields:
                raise RequestError("输入字段 id 重复或格式无效。")
            if kind != "file":
                validate_scalar(field, field["default"])
            self.fields[key] = field
        for token in command[1:]:
            if isinstance(token, str):
                continue
            if token == {"output_dir": True}:
                continue
            if not isinstance(token, dict) or token.get("field") not in self.fields:
                raise RequestError("command 引用了无效的字段。")
            if token.keys() == {"field"}:
                continue
            field = self.fields[token["field"]]
            if token.keys() != {"field", "flag"} or field["type"] != "boolean" or not isinstance(token["flag"], str):
                raise RequestError("flag 必须关联一个布尔字段。")
        self.runs = self.root / ".script-viewer" / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.active = None

    def source(self):
        if sha_file(self.path) != self.manifest_hash:
            raise RequestError("配置已改变。重启查看器后再运行。", 409)
        name = self.config["command"][0]
        candidate = str(self.cwd / name) if "/" in name else name
        executable = shutil.which(candidate)
        if not executable:
            raise RequestError(f"找不到可执行文件 {name}。")
        try:
            hashes = {name: sha_file((self.cwd / name).resolve()) for name in self.config["code_files"]}
        except OSError as error:
            raise RequestError(f"无法读取记录的源码文件 {error.filename}。") from error
        return {
            "manifest": self.manifest_hash,
            "files": hashes,
            "executable": str(Path(executable).absolute()),
            "resolved_executable": str(Path(executable).resolve()),
        }

    def public_config(self):
        return {
            "title": self.config["title"],
            "description": self.config.get("description", ""),
            "fields": [dict(field, sample=bool(field.get("sample"))) for field in self.fields.values()],
            "file_limit": FILE_LIMIT,
            "timeout": self.timeout,
        }

    def sample(self, key):
        relative = self.fields.get(key, {}).get("sample")
        if not relative:
            raise RequestError("示例不存在。", 404)
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise RequestError("示例不存在。", 404)
        with path.open("rb") as handle:
            data = handle.read(FILE_LIMIT + 1)
        if len(data) > FILE_LIMIT:
            raise RequestError("示例文件超过大小限制。")
        return {"name": path.name, "data": base64.b64encode(data).decode("ascii")}

    def run_dir(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise RequestError("运行记录不存在。", 404)
        path = self.runs / run_id
        if not path.is_dir() or path.is_symlink() or not (path / "run.json").is_file():
            raise RequestError("运行记录不存在。", 404)
        return path

    def describe(self, run):
        record = read_json(run / "run.json")
        if record["status"] == "running" and (self.active is None or self.active[0] != run):
            record["status"] = "interrupted"
        return record

    def history(self):
        with self.lock:
            records = [
                self.describe(path)
                for path in self.runs.iterdir()
                if RUN_ID.fullmatch(path.name) and not path.is_symlink() and (path / "run.json").is_file()
            ]
        return sorted(records, key=lambda item: (item["started"], item["sequence"]), reverse=True)[:100]

    def detail(self, run_id):
        with self.lock:
            run = self.run_dir(run_id)
            record = self.describe(run)
            log = run / "run.log"
            record["log"] = ""
            record["log_truncated"] = False
            if log.is_file():
                with log.open("rb") as handle:
                    size = handle.seek(0, os.SEEK_END)
                    handle.seek(max(0, size - LOG_LIMIT))
                    record["log"] = handle.read().decode("utf-8", errors="replace")
                    record["log_truncated"] = size > LOG_LIMIT
            record["outputs"] = []
            if record["status"] != "running":
                output = run / "outputs"
                if output.is_dir() and not output.is_symlink():
                    for path in sorted(output.rglob("*")):
                        if not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(output):
                            record["outputs"].append({"name": path.relative_to(output).as_posix(), "size": path.stat().st_size})
            return record

    def output(self, run_id, name):
        with self.lock:
            run = self.run_dir(run_id)
            path = resolve_output(run, name)
            if self.describe(run)["status"] == "running" or not path.is_file():
                raise RequestError("文件不存在或尚未写完。", 404)
            return path

    def start(self, values=None, replay_id=None):
        with self.lock:
            if self.active is not None:
                raise RequestError("已有任务运行，请等待结束或先取消。", 409)
            source = self.source()
            previous = None
            if replay_id:
                previous = self.run_dir(replay_id)
                original = self.describe(previous)
                if original["source"] != source:
                    raise RequestError("配置、受跟踪的源码或可执行文件路径已改变，无法原样复跑。", 409)
                values = read_json(previous / "inputs.json")
            if not isinstance(values, dict) or set(values) != set(self.fields):
                raise RequestError("提交的字段与当前配置不符。")

            run = self.runs / uuid.uuid4().hex
            output = run / "outputs"
            output.mkdir(parents=True)
            try:
                inputs, resolved = self.prepare_inputs(values, run, previous)
                cmd = self.command(source["executable"], resolved, output)
                write_json(run / "inputs.json", inputs)
                record = {
                    "id": run.name,
                    "status": "running",
                    "started": now(),
                    "sequence": time.time_ns(),
                    "finished": None,
                    "exit_code": None,
                    "parameters": inputs,
                    "source": source,
                    "replay_of": replay_id,
                }
                write_json(run / "run.json", record)
            except (RequestError, OSError):
                shutil.rmtree(run)
                raise

            try:
                with (run / "run.log").open("wb", buffering=0) as log:
                    process = subprocess.Popen(
                        cmd, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, start_new_session=True,
                        env=dict(os.environ, PYTHONUNBUFFERED="1", SCRIPT_RUN_OUTPUT_DIR=str(output)),
                    )
            except (OSError, ValueError) as error:
                (run / "run.log").write_text(str(error), encoding="utf-8")
                record.update(status="failed", finished=now())
                write_json(run / "run.json", record)
                return record

            self.active = (run, process, threading.Event())
            threading.Thread(target=self.wait, args=self.active, daemon=True).start()
            return record

    def prepare_inputs(self, values, run, previous):
        inputs, resolved = {}, {}
        for key, field in self.fields.items():
            value = values[key]
            if field["type"] != "file":
                inputs[key] = resolved[key] = validate_scalar(field, value)
                continue
            if not isinstance(value, dict):
                raise RequestError(f"请选择文件「{field['label']}」。")
            name = validate_filename(value.get("name"))
            accept = field.get("accept", [])
            if accept and not any(name.lower().endswith(suffix.lower()) for suffix in accept):
                raise RequestError(f"「{field['label']}」只接收 {'、'.join(accept)}。")
            if previous:
                try:
                    data = (previous / "inputs" / key / name).read_bytes()
                except OSError as error:
                    raise RequestError(f"保存的输入「{name}」已丢失。", 409) from error
                if sha(data) != value["sha256"]:
                    raise RequestError(f"保存的输入「{name}」已改变。", 409)
            else:
                try:
                    data = base64.b64decode(value.get("data", ""), validate=True)
                except (ValueError, TypeError, binascii.Error) as error:
                    raise RequestError("上传内容无法解码。") from error
            if len(data) > FILE_LIMIT:
                raise RequestError("单个文件不能超过 4 MiB。", 413)
            target = run / "inputs" / key / name
            target.parent.mkdir(parents=True)
            target.write_bytes(data)
            inputs[key] = {"name": name, "size": len(data), "sha256": sha(data)}
            resolved[key] = str(target)
        return inputs, resolved

    def command(self, executable, values, output):
        argv = [executable]
        for token in self.config["command"][1:]:
            if isinstance(token, str):
                argv.append(token)
            elif token == {"output_dir": True}:
                argv.append(str(output))
            elif "flag" in token:
                if values[token["field"]]:
                    argv.append(token["flag"])
            else:
                value = values[token["field"]]
                argv.append(str(value).lower() if type(value) is bool else str(value))
        return argv

    def wait(self, run, process, cancelled):
        status = None
        try:
            code = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            stop_process(process)
            code = process.wait()
            status = "timed_out"
        with self.lock:
            if cancelled.is_set():
                status = "cancelled"
            record = read_json(run / "run.json")
            record.update(status=status or ("succeeded" if code == 0 else "failed"), finished=now(), exit_code=code)
            try:
                write_json(run / "run.json", record)
            finally:
                self.active = None

    def cancel(self, run_id):
        with self.lock:
            if self.active is None or self.active[0].name != run_id or self.active[1].poll() is not None:
                raise RequestError("该任务已经结束。", 409)
            _, process, cancelled = self.active
            cancelled.set()
            stop_process(process)

    def close(self):
        with self.lock:
            if self.active is None:
                return
            run, process, cancelled = self.active
            cancelled.set()
            stop_process(process)
            record = read_json(run / "run.json")
            record.update(status="cancelled", finished=now(), exit_code=process.wait())
            write_json(run / "run.json", record)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond(self.get)

    def do_POST(self):
        self.respond(self.post)

    def respond(self, method):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        try:
            if host not in hosts or (origin is not None and origin != f"http://{host}"):
                raise RequestError("仅支持同源的本机请求。", 403)
            method(self.server.runner, unquote(urlsplit(self.path).path))
        except RequestError as error:
            self.json({"error": str(error)}, error.status)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def get(self, runner, path):
        if path in STATIC:
            name, content_type = STATIC[path]
            self.bytes((TEMPLATE / name).read_bytes(), content_type)
            return
        parts = path.strip("/").split("/")
        if parts == ["api", "config"]:
            self.json(runner.public_config())
        elif parts[:2] == ["api", "sample"] and len(parts) == 3:
            self.json(runner.sample(parts[2]))
        elif parts == ["api", "runs"]:
            self.json(runner.history())
        elif parts[:2] == ["api", "runs"] and len(parts) == 3:
            self.json(runner.detail(parts[2]))
        elif parts[:2] == ["api", "runs"] and len(parts) >= 5 and parts[3] == "outputs":
            file = runner.output(parts[2], "/".join(parts[4:]))
            with file.open("rb") as handle:
                self.headers_for(200, "application/octet-stream", os.fstat(handle.fileno()).st_size)
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(file.name, safe=""))
                self.end_headers()
                shutil.copyfileobj(handle, self.wfile)
        else:
            raise RequestError("页面不存在。", 404)

    def post(self, runner, path):
        parts = path.strip("/").split("/")
        if parts == ["api", "runs"]:
            if self.headers.get_content_type() != "application/json":
                raise RequestError("请求必须使用 JSON。", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise RequestError("无效的请求长度。") from error
            if not 0 < size <= REQUEST_LIMIT:
                raise RequestError("请求为空或超过 24 MiB。", 413)
            try:
                body = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeError, RecursionError) as error:
                raise RequestError("请求内容不是有效 JSON。") from error
            self.json(runner.start(values=body), 201)
        elif parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] in {"replay", "cancel"}:
            if self.headers.get_content_type() != "application/json":
                raise RequestError("请求必须使用 JSON。", 415)
            if parts[3] == "replay":
                self.json(runner.start(replay_id=parts[2]), 201)
            else:
                runner.cancel(parts[2])
                self.json({"ok": True})
        else:
            raise RequestError("页面不存在。", 404)

    def headers_for(self, status, content_type, size):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Referrer-Policy", "no-referrer")

    def bytes(self, body, content_type, status=200):
        self.headers_for(status, content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def log_message(self, format, *args):
        pass


def exit_on_signal(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=TEMPLATE / "viewer.json")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    try:
        runner = Runner(args.manifest)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.runner = runner
    except (OSError, KeyError, TypeError, ValueError, RequestError) as error:
        parser.exit(1, f"Viewer could not start: {error}\n")
    signal.signal(signal.SIGTERM, exit_on_signal)
    print(f"Script Run Viewer: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runner.close()


if __name__ == "__main__":
    main()
