"""高德 POI 2.0：单次显式查询；不会在页面访问时调用接口。"""
import json
import re
import socket
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class AmapError(Exception):
    pass


def text_value(value, limit=1000):
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


def ask_key():
    """本地星号输入框；不从网页收集、不写入配置或数据库。"""
    try:
        import tkinter as tk
        from tkinter import simpledialog, messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
    except Exception:
        raise AmapError("无法打开 Key 输入框，请使用本机标准 Python（含 tkinter）运行。") from None
    try:
        while True:
            value = simpledialog.askstring("旅游系统 · 高德数据导入", "请粘贴 Web 服务 Key（32 个英文字符或数字）。\n输入显示为星号，只用于本次请求，不保存。", show="*", parent=root)
            if value is None:
                return None
            value = value.strip()
            if re.fullmatch(r"[a-fA-F0-9]{32}", value):
                return value
            messagebox.showerror("输入检查", f"收到 {len(value)} 个字符；需要 32 位十六进制 Key。请重新复制 Key 列。", parent=root)
    finally:
        root.destroy()


def search(key, name, city):
    parameters = dict(key=key, keywords=name, region=city, city_limit="true", show_fields="business,photos", page_size=5, page_num=1, output="json")
    try:
        with urlopen(Request("https://restapi.amap.com/v5/place/text?" + urlencode(parameters)), timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise AmapError("接口返回内容过大，未导入。")
        data = json.loads(raw.decode("utf-8-sig"))
    except HTTPError as error:
        raise AmapError(f"HTTP 请求失败，状态码 {error.code}。未修改数据。") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise AmapError("网络连接失败或超时。未修改数据，请稍后再试。") from None
    except (ValueError, UnicodeError):
        raise AmapError("无法解析接口返回的数据。未修改数据。") from None
    if not isinstance(data, dict):
        raise AmapError("接口响应格式异常。")
    if str(data.get("status")) != "1":
        code = text_value(data.get("infocode"), 100).replace(key, "[隐藏]")
        info = text_value(data.get("info"), 200).replace(key, "[隐藏]")
        raise AmapError(f"高德接口未成功：{code} {info}。请检查权限与配额，不要反复运行。")
    pois = data.get("pois")
    if not isinstance(pois, list):
        raise AmapError("接口 pois 字段格式异常。")
    return [p for p in pois[:5] if isinstance(p, dict)]


def decimal_value(value, maximum):
    try:
        number = Decimal(text_value(value))
        if number.is_finite() and 0 <= number <= maximum:
            return number.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        pass
    return None


def normalize(poi):
    """空值、异常数值保留未知；消费不写入门票字段。"""
    business = poi.get("business")
    business = business if isinstance(business, dict) else {}
    values = {
        "poi_id": text_value(poi.get("id"), 64), "name": text_value(poi.get("name"), 300),
        "city": text_value(poi.get("cityname"), 100), "district": text_value(poi.get("adname"), 100),
        "address": text_value(poi.get("address")), "category": text_value(poi.get("type"), 500),
        "rating": decimal_value(business.get("rating"), 5),
        "average_cost": decimal_value(business.get("cost"), Decimal("99999999.99")),
        "phone": text_value(business.get("tel"), 300),
        "hours_today": text_value(business.get("opentime_today"), 5000),
        "hours_week": text_value(business.get("opentime_week"), 5000),
        "longitude": None, "latitude": None, "photos": [],
    }
    if not values["poi_id"] or not values["name"]:
        raise AmapError("选中的候选缺少 POI 编号或名称，未导入。")
    try:
        lon, lat = [Decimal(x.strip()) for x in text_value(poi.get("location"), 100).split(",")]
        if lon.is_finite() and lat.is_finite() and -180 <= lon <= 180 and -90 <= lat <= 90:
            values.update(longitude=float(lon), latitude=float(lat))
    except (ValueError, InvalidOperation):
        pass
    photos = poi.get("photos")
    if isinstance(photos, list):
        for item in photos[:10]:
            if not isinstance(item, dict):
                continue
            url = text_value(item.get("url"), 2001)
            try:
                parts = urlsplit(url)
                valid = len(url) <= 2000 and parts.scheme in ("https", "http") and parts.hostname and not parts.username and not parts.password
            except ValueError:
                valid = False
            if valid and url not in values["photos"]:
                values["photos"].append(url)
    return values
