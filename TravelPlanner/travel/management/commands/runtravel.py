"""本地开发启动器：Key 仅放进当前进程环境，不写到项目文件。"""
import os
import re
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from travel.amap_client import AmapError


def ask_key():
    try:
        import tkinter as tk
        from tkinter import simpledialog, messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
    except Exception:
        raise AmapError("无法打开本机输入框，请使用含 tkinter 的标准 Python 安装。") from None
    try:
        while True:
            value = simpledialog.askstring("山海之间 · 启动网页采集", "请粘贴高德 Web 服务 Key。\n只在本次网站运行期间使用，不写入文件或数据库。\n停止网站后，下次启动需重新输入。", show="*", parent=root)
            if value is None:
                return None
            value = value.strip()
            if re.fullmatch(r"[a-fA-F0-9]{32}", value):
                return value
            messagebox.showerror("输入检查", f"收到 {len(value)} 个字符，需要32位十六进制 Key。请复制 Key 列并使用英文输入法。", parent=root)
    finally:
        root.destroy()


def ask_tuniu_key():
    import tkinter as tk
    from tkinter import simpledialog, messagebox
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        while True:
            value = simpledialog.askstring("山海之间 · 途牛费用查询", "请粘贴途牛 API Key（不是高德 Key）。\n只用于本次网站运行，不写入文件或数据库。\n留空或取消可跳过在线票务查询，其他页面仍可使用。", show="*", parent=root)
            if value is None or not value.strip():
                return None
            value = value.strip()
            if len(value) >= 8 and all(33 <= ord(c) <= 126 for c in value) and '*' not in value:
                return value
            messagebox.showerror("输入检查", "请粘贴完整有效密钥，不要粘贴名称、星号或包含空格的内容。", parent=root)
    finally:
        root.destroy()


class Command(BaseCommand):
    help = "弹窗输入高德 Key 后，在本机启动旅游网站。"

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=8000)

    def handle(self, *args, **options):
        # TRAVEL_PLANNER_START_V5: 仅让高德域名直连，保留其他服务的代理设置。
        bypass = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
        hosts = [host.strip() for host in bypass.split(",") if host.strip()]
        if "restapi.amap.com" not in hosts:
            hosts.append("restapi.amap.com")
        os.environ["NO_PROXY"] = ",".join(hosts)
        os.environ["no_proxy"] = ",".join(hosts)
        if not 1024 <= options["port"] <= 65535:
            raise CommandError("端口需为 1024—65535。")
        key = os.environ.get("TRAVEL_AMAP_KEY", "")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", key):
            try:
                key = ask_key()
            except AmapError as error:
                raise CommandError(str(error)) from None
            if key is None:
                self.stdout.write("已取消启动。日常只浏览可运行 manage.py runserver。")
                return
            os.environ["TRAVEL_AMAP_KEY"] = key
        self.stdout.write("高德 Key 已用于本次启动，不写入项目文件。停止进程后需重新输入。")
        if not os.environ.get("TUNIU_API_KEY"):
            try:
                tuniu_key = ask_tuniu_key()
            except Exception:
                tuniu_key = None
            if tuniu_key:
                os.environ["TUNIU_API_KEY"] = tuniu_key
                os.environ["TUNIU_AUTH_TYPE"] = "apiKey"
        self.stdout.write("费用查询页面：http://127.0.0.1:" + str(options["port"]) + "/costs/")
        self.stdout.write("打开 http://127.0.0.1:8000/plan/ 可自动规划，无需先手工导入景点。")
        self.stdout.write("一体化旅行入口：http://127.0.0.1:" + str(options["port"]) + "/journey/")
        # 禁用重载避免重复弹窗；页面操作和文件保存功能正常。
        call_command("runserver", addrport=f"127.0.0.1:{options['port']}", use_reloader=False)
