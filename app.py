"""HodhodShare - انتقال فایل در شبکه محلی، بدون Cloud.

اجرا:  python app.py
"""

import base64
import ctypes
import io
import os
import re
import socket
import subprocess
import sys
import threading
import unicodedata
import urllib.request
import webbrowser
from pathlib import Path

import qrcode
from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

from translations import DEFAULT_LANG, LANGS, TEXT

PORT = 8080
APP_NAME = "HodhodShare"

# exe بدون پنجره کنسول ساخته می‌شود؛ در آن حالت stdout/stderr وجود ندارند.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

if getattr(sys, "frozen", False):
    # اجرا به صورت HodhodShare.exe (PyInstaller): صفحه‌ها داخل exe هستند،
    # پوشه shared کنار خود exe ساخته می‌شود.
    RESOURCE_DIR = Path(sys._MEIPASS)
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = BASE_DIR = Path(__file__).resolve().parent
SHARED_DIR = (BASE_DIR / "shared").resolve()      # کامپیوتر → گوشی
RECEIVED_DIR = (BASE_DIR / "received").resolve()  # گوشی → کامپیوتر
FOLDERS = {"shared": SHARED_DIR, "received": RECEIVED_DIR}
for _folder in FOLDERS.values():
    _folder.mkdir(exist_ok=True)

app = Flask(__name__,
            template_folder=str(RESOURCE_DIR / "templates"),
            static_folder=str(RESOURCE_DIR / "static"))
app.secret_key = os.urandom(24)


# ------------------------------------------------------------------ i18n

def current_lang():
    lang = request.cookies.get("lang")
    return lang if lang in LANGS else DEFAULT_LANG


def tr(key, **kwargs):
    return TEXT[current_lang()][key].format(**kwargs)


@app.context_processor
def inject_lang():
    lang = current_lang()
    return {"lang": lang, "dir": LANGS[lang], "t": TEXT[lang],
            "other_lang": "en" if lang == "fa" else "fa"}


@app.route("/lang/<code>")
def set_lang(code):
    if code not in LANGS:
        abort(404)
    target = request.args.get("next", "/")
    if target not in ("/", "/mobile"):  # فقط صفحه‌های خود برنامه، نه آدرس دلخواه
        target = "/"
    resp = redirect(target)
    resp.set_cookie("lang", code, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


# ---------------------------------------------------------------- network

physical_ips = []  # در Windows هنگام شروع برنامه پر می‌شود


def usable_ip(ip):
    return bool(ip) and not ip.startswith(("127.", "0.", "169.254."))


def find_physical_ips():
    """Windows: IP کارت‌های شبکه فیزیکی (Wi-Fi/Ethernet)، آنهایی که Gateway دارند اول.

    VPN، WSL و Hyper-V کارت شبکه مجازی می‌سازند که گوشی به آن دسترسی ندارد،
    پس کارت فیزیکی را به آنها ترجیح می‌دهیم.
    """
    if os.name != "nt":
        return []
    script = (
        "foreach ($n in Get-NetAdapter -Physical | ? Status -eq Up) {"
        " $gw = [int][bool](Get-NetRoute -InterfaceIndex $n.ifIndex"
        " -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue);"
        " Get-NetIPAddress -InterfaceIndex $n.ifIndex -AddressFamily IPv4"
        " -ErrorAction SilentlyContinue | % { \"$gw $($_.IPAddress)\" } }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = [line.split() for line in out.splitlines() if len(line.split()) == 2]
    rows.sort(key=lambda r: r[0], reverse=True)
    return [ip for _, ip in rows if usable_ip(ip)]


def candidate_ips():
    """IPهای شبکه محلی این کامپیوتر، بهترین گزینه اول."""
    found = []
    # اتصال UDP هیچ بسته‌ای ارسال نمی‌کند؛ فقط کارت شبکه مسیر را مشخص می‌کند.
    for target in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 1))
                found.append(s.getsockname()[0])
        except OSError:
            continue
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    found = [ip for ip in found if usable_ip(ip)]
    ordered = [ip for ip in physical_ips if ip in found] + found
    return list(dict.fromkeys(ordered))  # حذف تکراری‌ها با حفظ ترتیب


def get_local_ip():
    """IP داخلی سیستم را برمی‌گرداند، یا None اگر پیدا نشد."""
    ips = candidate_ips()
    return ips[0] if ips else None


def local_addresses():
    """همه آدرس‌هایی که متعلق به خود این کامپیوتر هستند."""
    return {"127.0.0.1", "::1", *candidate_ips()}


def require_local():
    """صفحه Desktop (آپلود/حذف) فقط از خود کامپیوتر قابل استفاده است."""
    if request.remote_addr not in local_addresses():
        abort(403)


def make_qr_data_uri(text):
    img = qrcode.make(text, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ files

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                  *(f"COM{i}" for i in range(1, 10)),
                  *(f"LPT{i}" for i in range(1, 10))}


def clean_filename(name):
    """نام فایل امن (با حفظ حروف فارسی) یا None اگر نام قابل قبول نیست."""
    if not name:
        return None
    name = unicodedata.normalize("NFC", name)
    name = name.replace("\\", "/").split("/")[-1]      # فقط نام، بدون مسیر
    name = INVALID_CHARS.sub("_", name)
    name = name.strip().strip(".").strip()              # بدون .. و فایل مخفی
    if not name or name.split(".")[0].upper() in RESERVED_NAMES:
        return None
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[:180 - len(ext)] + ext
    return name


def unique_path(folder, name):
    """اگر فایلی با این نام وجود داشت، ' (1)'، ' (2)' و ... اضافه می‌کند."""
    path = folder / name
    stem, ext = os.path.splitext(name)
    i = 1
    while path.exists():
        path = folder / f"{stem} ({i}){ext}"
        i += 1
    return path


def save_uploads(folder):
    """فایل‌های فرم را با نام امن در folder ذخیره می‌کند و تعدادشان را برمی‌گرداند."""
    saved = 0
    for f in request.files.getlist("files"):
        name = clean_filename(f.filename)
        if not name:
            continue
        f.save(unique_path(folder, name))
        saved += 1
    return saved


def folder_file(folder, name):
    """مسیر فایل داخل folder را برمی‌گرداند؛ در غیر این صورت 404."""
    if not name or clean_filename(name) != name:
        abort(404)
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        abort(404)
    return path


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


FILE_KINDS = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "svg"},
    "video": {"mp4", "mkv", "mov", "avi", "webm", "wmv"},
    "audio": {"mp3", "wav", "m4a", "flac", "ogg", "aac"},
    "pdf": {"pdf"},
    "archive": {"zip", "rar", "7z", "tar", "gz"},
    "doc": {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv"},
    "app": {"apk", "exe", "msi"},
}


def file_kind(ext):
    """دسته فایل، فقط برای رنگ برچسب در UI."""
    ext = ext.lower()
    return next((k for k, exts in FILE_KINDS.items() if ext in exts), "other")


def list_files(folder):
    files = []
    for p in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if p.is_file() and not p.name.startswith("."):
            files.append({"name": p.name,
                          "ext": p.suffix[1:5] or "file",
                          "kind": file_kind(p.suffix[1:]),
                          "size": human_size(p.stat().st_size)})
    return files


# ----------------------------------------------------------------- routes

@app.route("/")
def desktop():
    require_local()
    ips = candidate_ips()
    ip = request.args.get("ip")
    if ip not in ips:
        ip = ips[0] if ips else None
    mobile_url = f"http://{ip}:{PORT}/mobile" if ip else None
    return render_template(
        "desktop.html",
        ip=ip,
        other_ips=[x for x in ips if x != ip],
        port=PORT,
        mobile_url=mobile_url,
        qr=make_qr_data_uri(mobile_url) if mobile_url else None,
        files=list_files(SHARED_DIR),
        received=list_files(RECEIVED_DIR),
        received_dir=RECEIVED_DIR,
    )


@app.route("/upload", methods=["POST"])
def upload():
    require_local()
    saved = save_uploads(SHARED_DIR)
    flash(tr("flash_uploaded", n=saved) if saved else tr("flash_none"))
    return redirect(url_for("desktop"))


@app.route("/delete", methods=["POST"])
def delete():
    require_local()
    folder = FOLDERS.get(request.form.get("folder", "shared"))
    if folder is None:
        abort(404)
    path = folder_file(folder, request.form.get("name", ""))
    path.unlink()
    flash(tr("flash_deleted", name=path.name))
    return redirect(url_for("desktop"))


@app.route("/received/<path:name>")
def open_received(name):
    """باز کردن فایل دریافتی از گوشی در مرورگر کامپیوتر."""
    require_local()
    path = folder_file(RECEIVED_DIR, name)
    resp = send_file(path, download_name=path.name)
    # فایل از گوشی آمده و قابل اعتماد نیست: اگر HTML/SVG باشد، اسکریپتش اجرا نشود.
    resp.headers["Content-Security-Policy"] = "sandbox"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/open-folder", methods=["POST"])
def open_folder():
    """پوشه received را در File Explorer باز می‌کند."""
    require_local()
    if hasattr(os, "startfile"):
        os.startfile(RECEIVED_DIR)
    return redirect(url_for("desktop"))


@app.route("/state")
def state():
    """امضای وضعیت پوشه‌ها؛ صفحه Desktop با آن می‌فهمد فایل جدیدی آمده."""
    require_local()
    return {"sig": [[f["name"], f["size"]] for d in FOLDERS.values() for f in list_files(d)]}


@app.route("/mobile")
def mobile():
    return render_template("mobile.html", files=list_files(SHARED_DIR))


@app.route("/mobile/upload", methods=["POST"])
def mobile_upload():
    saved = save_uploads(RECEIVED_DIR)
    flash(tr("flash_sent", n=saved) if saved else tr("flash_none"))
    return redirect(url_for("mobile"))


@app.route("/download/<path:name>")
def download(name):
    path = folder_file(SHARED_DIR, name)
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/ping")
def ping():
    """اجرای دوم برنامه با این مسیر می‌فهمد برنامه از قبل در حال اجراست."""
    return APP_NAME


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """دکمه «خروج از برنامه»؛ چون پنجره کنسولی برای بستن وجود ندارد."""
    require_local()
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return render_template("closed.html")


@app.errorhandler(403)
def forbidden(_):
    return ("This page is only available on the host computer. Use /mobile instead.\n"
            "این صفحه فقط روی کامپیوتر میزبان در دسترس است. از /mobile استفاده کنید."), 403, {"Content-Type": "text/plain; charset=utf-8"}


# ------------------------------------------------------------------- main

def free_port(start):
    """اولین پورت آزاد از start به بعد (اگر 8080 گرفته بود، 8081 و ...)."""
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return None


def running_instance(start):
    """اگر HodhodShare از قبل در حال اجراست، پورت آن را برمی‌گرداند."""
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # بدون Proxy/VPN
    for port in range(start, start + 20):
        try:
            with direct.open(f"http://127.0.0.1:{port}/ping", timeout=0.5) as r:
                if r.read().decode() == APP_NAME:
                    return port
        except (OSError, ValueError):
            continue
    return None


def fail(message):
    print(f"\n{message}")
    if os.name == "nt":  # پنجره کنسول نیست؛ خطا را در پیام ویندوز نشان بده
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    sys.exit(1)


def main():
    global PORT
    existing = running_instance(PORT)
    if existing:
        webbrowser.open(f"http://127.0.0.1:{existing}/")
        return
    port = free_port(PORT)
    if port is None:
        fail(f"No free port found between {PORT} and {PORT + 19}.")
    PORT = port
    print("Detecting local network address...")
    physical_ips.extend(find_physical_ips())
    ip = get_local_ip()
    desktop_url = f"http://127.0.0.1:{PORT}/"

    print("=" * 50)
    print(" HodhodShare")
    print("=" * 50)
    print(f" Desktop page : {desktop_url}")
    if ip:
        print(f" Mobile URL   : http://{ip}:{PORT}/mobile")
    else:
        print(" Local IP not found - connect this PC to Wi-Fi/LAN.")
    print(f" Shared folder  : {SHARED_DIR}")
    print(f" Received folder: {RECEIVED_DIR}")
    print(" To stop: 'Exit' button on the desktop page, or Ctrl+C.")
    print("=" * 50)

    threading.Timer(1.0, lambda: webbrowser.open(desktop_url)).start()
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    except OSError as e:
        fail(f"Could not start server on port {PORT}: {e}")


if __name__ == "__main__":
    main()
