import base64
import html
import mimetypes
import os
import shlex
import shutil
import socket
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

from aiohttp import web

from sandbox.container import (
    IGNORE_DIRS,
    export_workspace_zip,
    resolve_workspace_path,
)

from .config import (
    get_nas_enable,
    get_nas_password,
    get_nas_port,
    get_nas_user,
    get_samba_enable,
    get_samba_port,
    get_workspace_dir,
)

SAMBA_CONTAINER_NAME = "sier_workspace_samba"


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_nas_status_summary() -> str:
    if not get_nas_enable():
        return "NAS: Disabled"
    ip = get_local_ip()
    port = get_nas_port()
    smb = f" | SMB:\\\\{ip}\\workspace" if get_samba_enable() else ""
    return f"NAS: http://{ip}:{port} (Web/DAV){smb}"


def ensure_samba_service() -> None:
    """SAMBA_ENABLE=True の場合、workspace/ を共有する軽量Sambaコンテナを起動する"""
    subprocess.run(["docker", "rm", "-f", SAMBA_CONTAINER_NAME], check=False, capture_output=True)
    if not get_samba_enable():
        return

    ws_dir = get_workspace_dir().resolve()
    user = get_nas_user() or "agent"
    password = get_nas_password()
    smb_port = get_samba_port()

    if not password:
        print("[NAS] Warning: NAS_PASSWORD is not set in .env. Skipping Samba startup for security.")
        return

    subprocess.run(["sudo", "-n", "systemctl", "stop", "smbd", "nmbd"], check=False, capture_output=True)

    uid = os.getuid()
    gid = os.getgid()

    smb_conf = (
        "[global]\n"
        "workgroup = WORKGROUP\n"
        "server string = SIer Workspace NAS\n"
        "security = user\n"
        "map to guest = Never\n"
        "ntlm auth = yes\n"
        "server min protocol = SMB2\n"
        "create mask = 0664\n"
        "directory mask = 0775\n"
        f"force user = {user}\n"
        "force group = nasgroup\n\n"
        "[workspace]\n"
        "path = /workspace\n"
        "browsable = yes\n"
        "writable = yes\n"
        "read only = no\n"
        f"valid users = {user}\n"
    )

    q_user = shlex.quote(user)
    q_pass = shlex.quote(password)
    q_conf = shlex.quote(smb_conf)

    setup_script = (
        "apk add --no-cache samba >/dev/null && "
        "mkdir -p /var/lib/samba/private /run/samba && "
        f"addgroup -g {gid} nasgroup 2>/dev/null || true && "
        f"adduser -D -H -u {uid} -G nasgroup {q_user} 2>/dev/null || true && "
        f"printf '%s\\n%s\\n' {q_pass} {q_pass} | smbpasswd -a -s {q_user} && "
        f"smbpasswd -e {q_user} && "
        f"printf '%s' {q_conf} > /etc/samba/smb.conf && "
        "smbd --foreground --no-process-group"
    )

    cmd = [
        "docker", "run", "-d",
        "--name", SAMBA_CONTAINER_NAME,
        "-p", f"{smb_port}:445",
        "-v", f"{ws_dir}:/workspace",
        "alpine:latest",
        "sh", "-c", setup_script,
    ]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if res.returncode == 0:
        print(f"[NAS] Samba server started: \\\\{get_local_ip()}\\workspace (user: {user})")
    else:
        subprocess.run(["docker", "rm", "-f", SAMBA_CONTAINER_NAME], check=False, capture_output=True)
        print(f"[NAS] Failed to start Samba container: {res.stderr.strip()}")


def stop_samba_service() -> None:
    subprocess.run(["docker", "rm", "-f", SAMBA_CONTAINER_NAME], check=False, capture_output=True)


# --- Basic Auth Middleware (NAS_USER / NAS_PASSWORD が設定されている場合に有効化) ---
@web.middleware
async def basic_auth_middleware(request: web.Request, handler):
    expected_user = get_nas_user()
    expected_pass = get_nas_password()
    if not expected_user or not expected_pass:
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            user, pw = decoded.split(":", 1)
            if user == expected_user and pw == expected_pass:
                return await handler(request)
        except Exception:
            pass

    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="SIer Workspace NAS"'},
        text="401 Unauthorized",
    )


# --- Web UI Handlers (iOS / Android / Windows ブラウザ対応) ---
async def handle_web_ui(request: web.Request) -> web.StreamResponse:
    rel_q = request.query.get("path", "")
    target = resolve_workspace_path(rel_q)
    ws_root = get_workspace_dir().resolve()

    if not target or not target.exists():
        raise web.HTTPNotFound(text="Path not found")

    if target.is_file():
        if request.query.get("download") == "1":
            return web.FileResponse(target)
        content = target.read_text(encoding="utf-8", errors="replace")
        rel_str = str(target.relative_to(ws_root))
        parent_str = "" if str(target.parent.relative_to(ws_root)) == "." else str(target.parent.relative_to(ws_root))
        page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Edit ~/{html.escape(rel_str)}</title>
        <style>body{{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:16px;margin:0}}
        textarea{{width:100%;height:72vh;background:#161b22;color:#f0f6fc;border:1px solid #30363d;padding:10px;box-sizing:border-box;font-family:monospace}}
        button,a.btn{{background:#238636;color:#fff;border:none;padding:8px 16px;text-decoration:none;border-radius:6px;cursor:pointer;display:inline-block;margin-right:8px}}
        a.sec{{background:#30363d}}</style></head><body>
        <h3>📄 ~/{html.escape(rel_str)}</h3>
        <form method="POST" action="/api/save">
          <input type="hidden" name="path" value="{html.escape(rel_str)}">
          <textarea name="content">{html.escape(content)}</textarea><br><br>
          <button type="submit">💾 Save</button>
          <a class="btn sec" href="/?path={urllib.parse.quote(rel_str)}&download=1">⬇ Download</a>
          <a class="btn sec" href="/?path={urllib.parse.quote(parent_str)}">⬅ Back</a>
        </form></body></html>"""
        return web.Response(text=page, content_type="text/html")

    rel_dir = "" if target == ws_root else str(target.relative_to(ws_root))
    rows: list[str] = []
    if target != ws_root:
        p_rel = "" if target.parent == ws_root else str(target.parent.relative_to(ws_root))
        rows.append(f'<tr><td>📁 <a href="/?path={urllib.parse.quote(p_rel)}">.. (Parent)</a></td><td>-</td><td>-</td></tr>')

    for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if item.name in IGNORE_DIRS:
            continue
        item_rel = str(item.relative_to(ws_root))
        q_path = urllib.parse.quote(item_rel)
        mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if item.is_dir():
            rows.append(f'<tr><td>📁 <a href="/?path={q_path}">{html.escape(item.name)}/</a></td><td>DIR</td><td>{mtime}</td></tr>')
        else:
            size_kb = f"{item.stat().st_size / 1024:.1f} KB"
            rows.append(
                f'<tr><td>📄 <a href="/?path={q_path}">{html.escape(item.name)}</a></td>'
                f'<td>{size_kb}</td><td>{mtime} | <a href="/?path={q_path}&download=1">⬇</a></td></tr>'
            )

    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>SIer Workspace NAS (~/)</title>
    <style>body{{background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:16px;max-width:960px;margin:auto}}
    a{{color:#58a6ff;text-decoration:none}} table{{width:100%;border-collapse:collapse;margin-top:12px}}
    th,td{{padding:10px;border-bottom:1px solid #21262d;text-align:left}}
    .bar{{display:flex;gap:8px;flex-wrap:wrap;background:#161b22;padding:12px;border-radius:8px;margin-bottom:12px}}
    input,button,a.btn{{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 12px;border-radius:6px}}</style></head><body>
    <h2>📂 Workspace NAS : ~/{html.escape(rel_dir)}</h2>
    <div class="bar">
      <form method="POST" action="/api/upload" enctype="multipart/form-data">
        <input type="hidden" name="dir" value="{html.escape(rel_dir)}">
        <input type="file" name="file" required>
        <button type="submit">⬆ Upload</button>
      </form>
      <form method="POST" action="/api/mkdir">
        <input type="hidden" name="dir" value="{html.escape(rel_dir)}">
        <input type="text" name="name" placeholder="New folder / file.txt" required>
        <button type="submit">➕ Create</button>
      </form>
      <a class="btn" href="/api/zip?path={urllib.parse.quote(rel_dir or '.')}">📦 Download ZIP</a>
    </div>
    <small>WebDAV Mount URL: <code>/dav/</code></small>
    <table><thead><tr><th>Name</th><th>Size</th><th>Modified</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table></body></html>"""
    return web.Response(text=page, content_type="text/html")


async def handle_api_save(request: web.Request) -> web.StreamResponse:
    data = await request.post()
    rel_path = str(data.get("path", ""))
    content = str(data.get("content", ""))
    target = resolve_workspace_path(rel_path)
    if not target or target.is_dir():
        raise web.HTTPBadRequest(text="Invalid file path")
    target.write_text(content, encoding="utf-8")
    raise web.HTTPFound(location=f"/?path={urllib.parse.quote(rel_path)}")


async def handle_api_upload(request: web.Request) -> web.StreamResponse:
    reader = await request.multipart()
    cur_dir = ""
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "dir":
            cur_dir = (await part.read(decode=True)).decode("utf-8", errors="replace")
        elif part.name == "file" and part.filename:
            rel_dest = f"{cur_dir}/{part.filename}".lstrip("/")
            target = resolve_workspace_path(rel_dest)
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as f:
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        f.write(chunk)
    raise web.HTTPFound(location=f"/?path={urllib.parse.quote(cur_dir)}")


async def handle_api_mkdir(request: web.Request) -> web.StreamResponse:
    data = await request.post()
    cur_dir = str(data.get("dir", ""))
    name = str(data.get("name", "")).strip()
    rel_dest = f"{cur_dir}/{name}".lstrip("/")
    target = resolve_workspace_path(rel_dest)
    if target:
        if "." in Path(name).name:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
    raise web.HTTPFound(location=f"/?path={urllib.parse.quote(cur_dir)}")


async def handle_api_zip(request: web.Request) -> web.StreamResponse:
    rel_path = request.query.get("path", ".")
    zip_path = export_workspace_zip(rel_path)
    return web.FileResponse(zip_path)


# --- WebDAV Handlers (/dav/... : Windows / iOS / Android ネイティブマウント用) ---
def _dav_prop_response(href: str, path: Path) -> ET.Element:
    resp = ET.Element("{DAV:}response")
    ET.SubElement(resp, "{DAV:}href").text = href
    propstat = ET.SubElement(resp, "{DAV:}propstat")
    prop = ET.SubElement(propstat, "{DAV:}prop")

    ET.SubElement(prop, "{DAV:}displayname").text = path.name or "workspace"
    res_type = ET.SubElement(prop, "{DAV:}resourcetype")
    if path.is_dir():
        ET.SubElement(res_type, "{DAV:}collection")
    else:
        stat = path.stat()
        ET.SubElement(prop, "{DAV:}getcontentlength").text = str(stat.st_size)
        mime, _ = mimetypes.guess_type(path.name)
        ET.SubElement(prop, "{DAV:}getcontenttype").text = mime or "application/octet-stream"

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    ET.SubElement(prop, "{DAV:}getlastmodified").text = format_datetime(mtime, usegmt=True)
    ET.SubElement(propstat, "{DAV:}status").text = "HTTP/1.1 200 OK"
    return resp


async def handle_webdav(request: web.Request) -> web.StreamResponse:
    tail = request.match_info.get("tail", "")
    target = resolve_workspace_path(tail)
    if not target:
        return web.Response(status=403, text="Forbidden")

    method = request.method.upper()
    if method == "OPTIONS":
        return web.Response(
            status=200,
            headers={
                "DAV": "1, 2",
                "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL, PROPFIND, MOVE, COPY",
                "MS-Author-Via": "DAV",
            },
        )

    if method == "PROPFIND":
        if not target.exists():
            return web.Response(status=404)
        depth = request.headers.get("Depth", "1")
        multistatus = ET.Element("{DAV:}multistatus")
        base_href = "/dav/" + tail.strip("/")
        if target.is_dir() and not base_href.endswith("/"):
            base_href += "/"
        multistatus.append(_dav_prop_response(base_href, target))

        if target.is_dir() and depth != "0":
            for child in sorted(target.iterdir()):
                if child.name in IGNORE_DIRS:
                    continue
                c_href = base_href + urllib.parse.quote(child.name) + ("/" if child.is_dir() else "")
                multistatus.append(_dav_prop_response(c_href, child))

        xml_bytes = ET.tostring(multistatus, encoding="utf-8", xml_declaration=True)
        return web.Response(status=207, body=xml_bytes, content_type="application/xml; charset=utf-8")

    if method in ("GET", "HEAD"):
        if not target.exists() or target.is_dir():
            return web.Response(status=404)
        return web.FileResponse(target)

    if method == "PUT":
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as f:
            async for chunk in request.content.iter_chunked(65536):
                f.write(chunk)
        return web.Response(status=201)

    if method == "MKCOL":
        target.mkdir(parents=True, exist_ok=True)
        return web.Response(status=201)

    if method == "DELETE":
        if not target.exists():
            return web.Response(status=404)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return web.Response(status=204)

    if method in ("MOVE", "COPY"):
        dest_hdr = request.headers.get("Destination", "")
        parsed = urllib.parse.urlparse(dest_hdr)
        dest_rel = urllib.parse.unquote(parsed.path)
        if "/dav/" in dest_rel:
            dest_rel = dest_rel.split("/dav/", 1)[1]
        dest_path = resolve_workspace_path(dest_rel)
        if not dest_path or not target.exists():
            return web.Response(status=400)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if method == "MOVE":
            shutil.move(str(target), str(dest_path))
        else:
            if target.is_dir():
                shutil.copytree(target, dest_path, dirs_exist_ok=True)
            else:
                shutil.copy2(target, dest_path)
        return web.Response(status=201)

    return web.Response(status=405)


class WorkspaceNASServer:
    def __init__(self) -> None:
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        ensure_samba_service()
        if not get_nas_enable():
            return

        app = web.Application(middlewares=[basic_auth_middleware], client_max_size=512 * 1024 * 1024)
        app.router.add_get("/", handle_web_ui)
        app.router.add_post("/api/save", handle_api_save)
        app.router.add_post("/api/upload", handle_api_upload)
        app.router.add_post("/api/mkdir", handle_api_mkdir)
        app.router.add_get("/api/zip", handle_api_zip)
        app.router.add_route("*", "/dav", handle_webdav)
        app.router.add_route("*", "/dav/{tail:.*}", handle_webdav)

        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        port = get_nas_port()
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        print(f"[NAS] WebUI & WebDAV started on http://{get_local_ip()}:{port} (DAV: /dav/)")

    async def stop(self) -> None:
        stop_samba_service()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


nas_server = WorkspaceNASServer()