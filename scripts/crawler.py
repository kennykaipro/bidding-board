#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
招投标信息抓取程序（多源版 v2）
- 抓取多个公开招标信息来源，统一过滤（4省 + 业务关键词 + 阶段多选 + 日期范围）
- 支持用户自定义源（sources_config.json）：CSS 选择器 / 正则 / JSON-API / Cookie 登录 / 分页
- 支持关键词及"类关键词"（同义词组）管理、日期范围（近1周/1月/3月/6月/1年/自定义）
- 输出 data/data.json 与 index.html（数据内嵌，纯静态前端筛选）

已接入源：
  1. ccgp   中国政府采购网·地方公告（政府源）7 栏目
  2. csg    南方电网供应链统一服务平台（央企源）6 栏目
  3. szggzy 深圳公共资源交易中心（政府源·JSON API：政府采购/建设工程/土地矿业/产权/拍卖/集体要素/医用耗材）

用法: python3 crawler.py [--days N] [--out-dir 目录]
"""
import argparse
import base64
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests

# 全局 socket 超时：防止 DNS/TCP/TLS 建立阶段挂起（南网等站点会拖住连接直到默认超时）
socket.setdefaulttimeout(8)
warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

# ============ 内置源配置 ============
SOURCES = [
    {
        "key": "ccgp",
        "name": "中国政府采购网",
        "kind": "政府",
        "base": "http://www.ccgp.gov.cn/cggg/dfgg/",
        "page_depth": 3,
        "columns": [
            ("gkzb", "招标公告"),
            ("jzxcs", "竞争性磋商"),
            ("jzxtpgg", "竞争性谈判"),
            ("zbgg", "中标公告"),
            ("cjgg", "成交公告"),
            ("gzgg", "更正公告"),
            ("qtgg", "其他公告"),
        ],
    },
    {
        "key": "csg",
        "name": "南方电网供应链统一服务平台",
        "kind": "央企",
        "base": "https://www.bidding.csg.cn/",
        "page_depth": 1,
        "use_requests": True,
        "retries": 1,
        "columns": [
            ("zbgg", "招标公告"),
            ("zbcg", "采购公告"),
            ("fzbgg", "非招标公告"),
            ("zbhxrgs", "公示公告"),
            ("xygg", "寻源公告"),
            ("lxcggg", "零星采购公告"),
        ],
    },
]

# 深圳公共资源交易中心：JSON API 源（公告类型字段 jygg_gglxmc_rank1 不可靠，阶段由标题判断）
SZGGZY = {
    "key": "szggzy",
    "name": "深圳公共资源交易中心",
    "kind": "政府",
    "type": "api",
    "api_url": "https://www.szggzy.com/cms/api/v1/trade/content/page",
    "channels": [2850, 2851, 2852, 2853, 2854, 2855, 2856],
    "channel_names": {2850: "政府采购", 2851: "建设工程", 2852: "土地矿业", 2853: "产权交易",
                      2854: "拍卖交易", 2855: "集体企业要素", 2856: "医用耗材"},
}

# ============ 默认全局设置（可被 sources_config.json 的 settings 覆盖） ============
DEFAULT_SETTINGS = {
    "date_range_days": 7,      # 默认收集近 7 天；可选 7/30/90/180/365/自定义
    "stages": [],              # 空 = 全部阶段；多选如 ["规划","招标","中标"]
    "auto_detect": True,       # 选择器为空时允许自动探测（纯规则，0 积分）；False = 直接跳过
}

# 阶段判断规则（按优先级：更正 > 中标/成交 > 规划 > 招标）
STAGE_RULES = [
    ("更正", ["更正", "澄清", "答疑", "延期", "终止", "废标", "流标", "暂停", "变更"]),
    ("中标", ["中标", "成交", "结果公告", "定标", "中选", "候选人", "评标公示", "中签", "成交结果"]),
    ("规划", ["规划", "可研", "可行性研究", "立项", "预公告", "需求公示", "意向公开", "勘察",
              "设计招标", "设计项目", "环评", "用地预审", "概算", "评审专家征集", "征集公告"]),
    ("招标", ["招标", "采购", "磋商", "谈判", "询价", "比选", "遴选", "招商", "征集", "拍卖",
              "竞价", "挂牌", "出让", "招募", "报名", "投标邀请"]),
]

# 默认关键词（类关键词：一组同义词命中任一词即收录；可被 sources_config.json 的 keywords 覆盖）
DEFAULT_KEYWORDS = [
    {"group": "信息化/数字化", "words": ["信息化", "数字化", "政务云", "云计算", "信创", "数据中心", "数据中台", "数据治理", "大数据"]},
    {"group": "智能化/AI", "words": ["智能化", "人工智能", "智能体", "AI", "大模型", "智慧", "智慧园区", "智慧城市", "智慧校园", "智能"]},
    {"group": "音视频/多媒体", "words": ["音视频", "音像", "多媒体", "视频会议", "会议系统", "录播", "音响", "广播", "舞台灯光", "声光", "电子显示"]},
    {"group": "安防监控", "words": ["安防", "监控", "视频", "门禁", "报警", "消防"]},
    {"group": "显示设备", "words": ["LED", "大屏", "显示屏", "电子屏", "屏幕", "教学一体机", "智慧黑板", "触控一体机", "显示设备"]},
    {"group": "系统集成/弱电", "words": ["系统集成", "弱电", "机房", "服务器", "网络", "存储", "指挥中心", "调度中心", "综合布线"]},
]

# ============ 省份过滤（标题文本匹配） ============
PROVINCES = {
    "广东": ["广东", "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆",
            "江门", "茂名", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞",
            "中山", "潮州", "揭阳", "云浮"],
    "浙江": ["浙江", "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州",
            "舟山", "台州", "丽水"],
    "江苏": ["江苏", "南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安",
            "盐城", "扬州", "镇江", "泰州", "宿迁"],
    "山东": ["山东", "济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁",
            "泰安", "威海", "日照", "临沂", "德州", "聊城", "滨州", "菏泽"],
    "山西": ["山西", "太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城", "忻州", "临汾", "吕梁"],
    "福建": ["福建", "福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德"],
    "海南": ["海南", "海口", "三亚", "三沙", "儋州", "五指山", "文昌", "琼海", "万宁", "东方",
            "定安", "屯昌", "澄迈", "临高", "白沙", "昌江", "乐东", "陵水", "保亭", "琼中"],
    "北京": ["北京", "北京市", "东城", "西城", "朝阳", "海淀", "丰台", "石景山", "门头沟", "房山", "通州", "顺义", "昌平", "大兴", "怀柔", "平谷", "密云", "延庆"],
    "上海": ["上海", "上海市", "黄浦", "徐汇", "长宁", "静安", "普陀", "虹口", "杨浦", "闵行", "宝山", "嘉定", "浦东", "金山", "松江", "青浦", "奉贤", "崇明"],
    "天津": ["天津", "天津市", "和平", "河东", "河西", "南开", "红桥", "东丽", "西青", "津南", "北辰", "武清", "宝坻", "滨海", "宁河", "静海", "蓟州"],
    "重庆": ["重庆", "重庆市", "渝中", "大渡口", "江北", "沙坪坝", "九龙坡", "南岸", "北碚", "渝北", "巴南", "涪陵", "长寿", "江津", "合川", "永川", "南川", "綦江", "大足", "璧山", "万州", "黔江"],
    "安徽": ["安徽", "合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州", "阜阳", "宿州", "六安", "亳州", "池州", "宣城"],
    "宁夏": ["宁夏", "银川", "石嘴山", "吴忠", "固原", "中卫"],
    "湖北": ["湖北", "武汉", "黄石", "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州", "黄冈", "咸宁", "随州", "恩施", "仙桃", "潜江", "天门", "神农架"],
    "黑龙江": ["黑龙江", "哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯", "七台河", "牡丹江", "黑河", "绥化", "大兴安岭"],
    "吉林": ["吉林", "长春", "四平", "辽源", "通化", "白山", "松原", "白城", "延边"],
    "辽宁": ["辽宁", "沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛"],
    "河北": ["河北", "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水"],
    "江西": ["江西", "南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春", "抚州", "上饶"],
    "湖南": ["湖南", "长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州", "永州", "怀化", "娄底", "湘西"],
    "四川": ["四川", "成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江", "乐山", "南充", "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳", "阿坝", "甘孜", "凉山"],
    "贵州": ["贵州", "贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁", "黔西南", "黔东南", "黔南"],
    "云南": ["云南", "昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧", "楚雄", "红河", "文山", "西双版纳", "大理", "德宏", "怒江", "迪庆"],
    "陕西": ["陕西", "西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛"],
    "甘肃": ["甘肃", "兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉", "庆阳", "定西", "陇南", "临夏", "甘南"],
    "青海": ["青海", "西宁", "海东", "海北", "黄南", "果洛", "玉树", "海西"],
    "新疆": ["新疆", "乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "昌吉", "博尔塔拉", "巴音郭楞", "阿克苏", "克孜勒苏", "喀什", "和田", "伊犁", "塔城", "阿勒泰"],
    "内蒙古": ["内蒙古", "呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔", "巴彦淖尔", "乌兰察布", "兴安", "锡林郭勒", "阿拉善"],
    "广西": ["广西", "南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林", "百色", "贺州", "河池", "来宾", "崇左"],
    "河南": ["河南", "郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店", "济源"],
    "西藏": ["西藏", "拉萨", "日喀则", "昌都", "林芝", "山南", "那曲", "阿里"],
}

# ============ 用户自定义源配置 ============
# sources_config.json：公开配置（存于 GitHub 仓库，用户可自助编辑/提交）
#   version 2 结构: {"version":2,"settings":{...},"keywords":[...],"sources":[...]}
# sources_config.local.json：本地敏感配置（cookies 等，不上传，不公开）
PUBLIC_CONFIG = "sources_config.json"
LOCAL_CONFIG = "sources_config.local.json"


def pull_custom_config(root: str):
    """启动时从 GitHub 拉取最新 sources_config.json 覆盖本地，实现用户自助添加即时生效。"""
    config_path = os.path.join(root, "scripts", "config.json")
    if not os.path.exists(config_path):
        return
    try:
        cfg = json.load(open(config_path, encoding="utf-8"))
        token, repo = cfg.get("github_token"), cfg.get("github_repo")
        branch = cfg.get("github_branch", "main")
        if not (token and repo):
            return
        url = f"https://api.github.com/repos/{repo}/contents/{PUBLIC_CONFIG}?ref={branch}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "User-Agent": "bidding-crawler",
            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
            content = base64.b64decode(meta["content"]).decode("utf-8")
            with open(os.path.join(root, PUBLIC_CONFIG), "w", encoding="utf-8") as f:
                f.write(content)
            print("[config] 已从 GitHub 拉取 sources_config.json")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[config] GitHub 无 sources_config.json，使用本地配置")
        else:
            print(f"[config] 拉取失败: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        print(f"[config] 拉取失败: {e}")


def load_board_config(root: str):
    """读取全局配置：settings（日期范围/阶段多选/AI 探测开关）+ keywords + custom sources。
    本地 sources_config.local.json 可携带 cookies 与自动探测回填的选择器（detected_selectors）。"""
    settings = dict(DEFAULT_SETTINGS)
    keywords = [dict(k) for k in DEFAULT_KEYWORDS]
    custom_sources = []
    public_path = os.path.join(root, PUBLIC_CONFIG)
    if os.path.exists(public_path):
        try:
            data = json.load(open(public_path, encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("settings"), dict):
                    settings.update({k: v for k, v in data["settings"].items() if v is not None})
                if isinstance(data.get("keywords"), list) and data["keywords"]:
                    keywords = [dict(k) for k in data["keywords"]]
                if isinstance(data.get("sources"), list):
                    custom_sources = data["sources"]
        except Exception as e:  # noqa: BLE001
            print(f"[config] 解析 {PUBLIC_CONFIG} 失败: {e}")
    # 兼容旧版结构：只含 sources 数组
    if not custom_sources:
        try:
            data = json.load(open(public_path, encoding="utf-8"))
            if isinstance(data, list):
                custom_sources = data
        except Exception:  # noqa: BLE001
            pass
    # 本地敏感配置（cookies + 自动探测回填的选择器）
    local_path = os.path.join(root, LOCAL_CONFIG)
    cookies = {}
    detected = {}
    if os.path.exists(local_path):
        try:
            ldata = json.load(open(local_path, encoding="utf-8"))
            cookies = ldata.get("cookies", {}) or {}
            detected = ldata.get("detected_selectors", {}) or {}
            if isinstance(ldata.get("settings"), dict):
                # 本地可覆盖 AI 探测总开关等设置
                for k, v in ldata["settings"].items():
                    if v is not None:
                        settings[k] = v
        except Exception:  # noqa: BLE001
            pass
    for s in custom_sources:
        s["kind"] = s.get("kind", "自定义")
        key = s.get("key", "")
        if key in cookies and isinstance(cookies[key], str) and cookies[key]:
            s["cookies"] = cookies[key]
        # 自动探测回填：合并到该站点的 parse（站点仍无选择器时生效）
        if key in detected and isinstance(detected[key], dict):
            s["_detected_parse"] = detected[key]
    return settings, keywords, custom_sources


def fetch_with_requests(url: str, cookies: str = None, retries: int = 1) -> str:
    """基于 requests 的抓取，支持 Cookie 头（登录源）与完整浏览器头；带快速重试。
    禁用连接级重试（max_retries=0），避免南网等站点在 TCP/TLS 阶段无限重连拖时间。"""
    headers = dict(BROWSER_HEADERS)
    if "bidding.csg.cn" in url:
        headers["Referer"] = "https://www.bidding.csg.cn/"
    if cookies:
        headers["Cookie"] = cookies
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=headers, timeout=6, verify=False)
            resp.raise_for_status()
            if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1",):
                resp.encoding = "utf-8"
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.0 + random.random())
    raise last


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def fetch(url: str, retries: int = 3) -> str:
    """带完整浏览器头与重试的抓取；南网等站点对 urllib 特征会随机拦截。"""
    last = None
    for attempt in range(1, retries + 1):
        headers = dict(BROWSER_HEADERS)
        if "bidding.csg.cn" in url:
            headers["Referer"] = "https://www.bidding.csg.cn/"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
                return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(2.0 * attempt + random.random())
    raise last


# 各源解析正则
RE_CCGP = re.compile(
    r'<a href="\./(\d{6})/t(\d{8})_(\d+)\.htm"[^>]*title="([^"]*)"',
    re.IGNORECASE,
)
RE_CSG = re.compile(
    r'<li><span class="Right"><a href="[^"]*"[^>]*>[^<]*</a>\s*\|[^<]*<span[^>]*>'
    r'([0-9-]{10})</span></span><span\s*class="num"><i>\d+</i></span>'
    r'<a[^>]*class="Blue">([^<]*)</a>.*?<a href="([^"]*\.jhtml)"[^>]*>([^<]*)</a></li>',
    re.S,
)


def abs_url(list_url: str, href: str, link_prefix: str = "") -> str:
    """把相对链接拼接为绝对链接（404 修复）：
    已带 http(s) 原样返回；// 协议相对补 https:；优先用 link_prefix；
    否则用 list_url 的域名/目录自动拼接。"""
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if link_prefix:
        base = link_prefix.rstrip("/")
        if not base:
            return href
        return base + (href if href.startswith("/") else "/" + href.lstrip("/"))
    try:
        return urljoin(list_url or "", href)
    except Exception:  # noqa: BLE001
        return href


def is_dynamic_page(html: str) -> bool:
    """检测 JS 动态渲染页面（SPA）：静态爬虫拿不到列表内容 → 直接跳过，不消耗任何积分。"""
    if not html:
        return True
    low = html.lower()
    # 服务端模板占位符（Vue/{{ }} 未渲染）：链接文本是占位表达式，说明静态 HTML 无真实列表
    placeholder_links = len(re.findall(r'<a[^>]*>[^<]*\{\{', html))
    if placeholder_links >= 3:
        return True
    # SPA 框架特征
    has_framework = any(k in low for k in [
        "id=\"app\"", "id='app'", "id=\"root\"", "id='root'", "__nuxt__", "__next__",
        "__initial_state__", "__preloaded_state__", "createapp(", "reactdom", "ng-app", "ng-app",
    ])
    if not has_framework:
        return False
    # 若页面上已有可解析的静态链接，则不是纯动态页
    href_count = len(re.findall(r'<a[^>]+href=["\']', html, re.I))
    return href_count < 3


# 启发式选择器探测（不调用 LLM，0 积分）：空选择器时尝试常见列表结构
COMMON_ITEM_SELECTORS = [
    "ul.list > li", ".list > li", "ul.news-list > li", ".news-list > li",
    ".item-list li", ".article-list li", ".list li", "table tbody tr",
    "table tr", "ul > li", "li",
]
COMMON_TITLE_SELECTORS = ["a[title]", "h3 a", "h4 a", ".title a", "a.link", "a"]
COMMON_DATE_SELECTORS = [".date", ".time", "span.date", ".t", "em", "td:last-child"]


def auto_detect_selectors(html: str, list_url: str):
    """启发式探测 item/title/date/link 选择器。返回 dict 或 None（探测失败）。
    纯规则实现，不调用大模型，不消耗积分。"""
    soup = BeautifulSoup(html, "lxml")
    for item_sel in COMMON_ITEM_SELECTORS:
        try:
            nodes = soup.select(item_sel)
        except Exception:  # noqa: BLE001
            continue
        if not nodes:
            continue
        for t_sel in COMMON_TITLE_SELECTORS:
            valid = 0
            for node in nodes[:10]:
                a = node.select_one(t_sel) if t_sel else node.find("a")
                if a is None:
                    a = node.find("a")
                if a is None:
                    continue
                title = a.get_text(strip=True)
                href = a.get("href") or ""
                if title and href and "{{" not in title:
                    valid += 1
            if valid >= 3:
                d_sel = ""
                for cand in COMMON_DATE_SELECTORS:
                    hits = sum(1 for node in nodes[:10] if node.select_one(cand))
                    if hits >= 3:
                        d_sel = cand
                        break
                return {"item": item_sel, "title": t_sel, "date": d_sel,
                        "link": t_sel}
    return None


def dynamic_depth(days: int):
    """按回溯天数决定每源翻页深度（页数上限）。API 单窗口 total 封顶 1000（size=100 → 20 页），
    25 页保证覆盖；提前停逻辑（0 新增）会在翻出日期范围时截断，不浪费请求。
    窗口大于 7 天时加深翻页，确保全国地方公告能翻到更早的直辖市/省份数据。"""
    if days <= 7:
        return 25
    if days <= 30:
        return 60
    return 120


def parse_source(src, cutoff: str, days: int = 7):
    """抓取内置 HTML 源（ccgp/csg），返回条目列表。"""
    base = src["base"].rstrip("/") + "/"
    use_requests = src.get("use_requests", False)
    fetch_retries = src.get("retries", 3)
    page_depth = src.get("page_depth", 1)
    if src["key"] == "ccgp":
        page_depth = dynamic_depth(days)
    items = []
    for code, stage in src["columns"]:
        col_url = base + code + "/"
        for page in range(1, page_depth + 1):
            url = col_url if page == 1 else f"{col_url}index_{page}.htm"
            try:
                html = fetch_with_requests(url, retries=fetch_retries) if use_requests else fetch(url, retries=fetch_retries)
            except Exception as exc:  # noqa: BLE001
                items.append({"error": f"{url}: {exc}"})
                continue
            page_start = len(items)
            if src["key"] == "ccgp":
                for m in RE_CCGP.finditer(html):
                    _month, date_str, doc_id, title = m.groups()
                    if date_str < cutoff:
                        continue
                    rel = f"./{_month}/t{date_str}_{doc_id}.htm"
                    items.append({
                        "title": title.strip(),
                        "url": base + rel[2:],
                        "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
                        "stage": stage,
                    })
            elif src["key"] == "csg":
                for m in RE_CSG.finditer(html):
                    date_str, buyer, href, title = m.groups()
                    if date_str.replace("-", "") < cutoff:
                        continue
                    if not href.startswith("http"):
                        href = "https://www.bidding.csg.cn" + href
                    items.append({
                        "title": title.strip(),
                        "url": href,
                        "date": date_str,
                        "stage": stage,
                        "buyer": buyer.strip(),
                    })
            # 本页未新增任何条目（全部早于 cutoff）→ 无需再翻页
            if len(items) == page_start:
                break
        # 栏目间随机延时，规避反爬
        time.sleep(0.5 + random.random() * 0.7)
    return items


def fetch_api_json(url: str, body: dict, retries: int = 3, form: bool = False, referer: str = "",
                   sign_key: str = ""):
    """POST JSON 接口抓取（深圳公共资源交易中心模式）。
    form=True 时以 application/x-www-form-urlencoded 提交（webBuilder 系列接口）。
    sign_key 非空时启用福建式签名：body 注入毫秒时间戳 ts，
    对排除空值后的参数按 key 大写排序拼接（对象/数组用 JSON 序列化），
    计算 MD5(密钥+拼接串) 小写作 portal-sign 头。"""
    headers = dict(BROWSER_HEADERS)
    if form:
        headers.update({"Content-Type": "application/x-www-form-urlencoded"})
        if referer:
            headers["Referer"] = referer
        payload = body
    else:
        headers.update({"Content-Type": "application/json",
                        "Referer": referer or "https://www.szggzy.com/xxgk/list.html"})
        payload = json.dumps(body)
    if sign_key:
        ts = int(time.time() * 1000)
        if form:
            body["ts"] = ts
            payload = body
        else:
            body = dict(body)
            body["ts"] = ts
            payload = json.dumps(body)
        # 排除空值后签名
        p = {k: v for k, v in body.items() if v not in ("", None)}
        concat = ""
        for k in sorted(p.keys(), key=lambda k: k.upper()):
            v = p[k]
            concat += k + (json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v))
        headers["portal-sign"] = hashlib.md5((sign_key + concat).encode()).hexdigest().lower()
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, headers=headers, data=payload, timeout=12, verify=False)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.5 * attempt + random.random())
    raise last


def parse_szggzy(src, cutoff: str, end_date: str, days: int = 7):
    """深圳公共资源交易中心 JSON API 源。日期范围用接口参数精确控制，分页拉取。
    回溯天数大时按周分段深挖（深圳接口支持 releaseTimeBegin/End，逐段翻页可覆盖更久历史）；
    单段内翻页直到本页 0 新增或覆盖到段首。"""
    api_url = src["api_url"]
    items = []
    max_pages = dynamic_depth(days)
    cutoff_num = cutoff.replace("-", "")
    # 分段：days<=14 不分段；否则按 7 天一段（单日查询 API 语义异常，7 天窗口更稳），从 end 往 cutoff 排
    segments = []
    if days <= 14:
        segments.append((cutoff, end_date))
    else:
        step = 7
        seg_end = end_date
        seg_begin = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=step - 1)).strftime("%Y-%m-%d")
        while seg_begin >= cutoff:
            segments.append((seg_begin, seg_end))
            seg_end = (datetime.strptime(seg_begin, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            seg_begin = (datetime.strptime(seg_begin, "%Y-%m-%d") - timedelta(days=step)).strftime("%Y-%m-%d")
        if seg_begin < cutoff:
            last = (datetime.strptime(seg_end, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            if last >= cutoff:
                segments.append((cutoff, last))
    for ch in src.get("channels", []):
        for seg_begin, seg_end in segments:
            seg_seen = set()
            page = 0
            while page < max_pages:
                body = {
                    "channelId": ch,
                    "fields": [],
                    "title": None,
                    "releaseTimeBegin": seg_begin,          # YYYY-MM-DD
                    "releaseTimeEnd": seg_end,
                    "page": page,
                    "size": 100,
                }
                try:
                    data = fetch_api_json(api_url, body)
                except Exception as exc:  # noqa: BLE001
                    items.append({"error": f"{api_url} channel={ch}: {exc}"})
                    break
                content = (data or {}).get("data", {}).get("content", []) or []
                if not content:
                    break
                page_dates = []
                page_new = 0
                for it in content:
                    title = it.get("title") or ""
                    if not title:
                        continue
                    date = str(it.get("releaseTime") or "")[:10]
                    if date:
                        page_dates.append(date)
                    if date and date.replace("-", "") < cutoff_num:
                        continue
                    key = (title.strip(), date)
                    if key in seg_seen:
                        continue
                    seg_seen.add(key)
                    page_new += 1
                    href = it.get("linkTo") or ""
                    if not href:
                        cid = it.get("contentId") or it.get("id") or ""
                        if cid:
                            href = f"https://www.szggzy.com/jygg/details.html?contentId={cid}"
                    if href and not href.startswith("http"):
                        href = "https://www.szggzy.com/" + href.lstrip("/")
                    items.append({
                        "title": title.strip(),
                        "url": href,
                        "date": date,
                        "stage": src["channel_names"].get(ch, "公告"),
                        "area": (it.get("areaName") or "") if isinstance(it, dict) else "",
                        "channel": src["channel_names"].get(ch, ""),
                    })
                # 本页无新增（已翻完或全是段内重复）→ 该段翻完
                if page_new == 0:
                    break
                # 本页所有日期都早于段起始日期 → 已翻到范围外，提前停止
                if page_dates and all(d.replace("-", "") < seg_begin.replace("-", "") for d in page_dates):
                    break
                page += 1
                time.sleep(0.2 + random.random() * 0.3)
    return items


def save_detected_selector(root: str, key: str, parse: dict):
    """把自动探测成功的选择器回填到本地配置 sources_config.local.json，下次采集直接复用（0 积分）。"""
    local_path = os.path.join(root, LOCAL_CONFIG)
    data = {}
    if os.path.exists(local_path):
        try:
            data = json.load(open(local_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    detected = data.get("detected_selectors", {}) or {}
    detected[key] = parse
    data["detected_selectors"] = detected
    try:
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def parse_custom_source(src, cutoff: str, root: str = "", auto_detect: bool = True):
    """通用源解析：CSS 选择器（bs4）/ 正则 / JSON-API，可选 Cookie 与分页。
    返回 (items, stats)；stats 含成功/失败条数、静态/动态标记、AI 调用次数、探测与复用标记。
    - 手填完整选择器：直接解析，0 AI 调用；
    - 选择器为空且开关开启：启发式探测一次并回填本地配置，下次复用（0 积分，不调 LLM）；
    - 选择器为空且开关关闭：直接跳过；
    - JS 动态渲染页面：红色提示跳过，禁止反复探测。"""
    stats = {"ok": 0, "fail": 0, "dynamic": False, "ai_calls": 0,
             "detected": False, "skipped": False, "reused": False}
    list_url = src.get("list_url") or ""
    if not list_url and src.get("type") != "api":
        return [{"error": f"{src.get('name','?')}: 缺少 list_url"}], stats
    parse = dict(src.get("parse") or {})
    # 自动探测回填的选择器优先复用（本地已存，不消耗积分）
    detected_parse = src.get("_detected_parse") or {}
    ptype = parse.get("type", "css")
    if ptype == "api_json":
        end_date = datetime.now().strftime("%Y-%m-%d")
        items = _parse_api_json_custom(src, cutoff, end_date)
        stats["ok"] = sum(1 for it in items if "error" not in it)
        stats["fail"] = sum(1 for it in items if "error" in it)
        return items, stats
    page_depth = int(src.get("page_depth", 1) or 1)
    page_pattern = src.get("page_url_pattern", "") or ""
    default_stage = src.get("stage", "公告")
    link_prefix = parse.get("link_prefix", "") or ""
    items = []
    # 站点域名（404 修复：相对链接用它拼接）
    site_origin = ""
    try:
        pu = urlparse(list_url)
        site_origin = f"{pu.scheme}://{pu.netloc}" if pu.scheme and pu.netloc else ""
    except Exception:  # noqa: BLE001
        site_origin = ""
    # CSS 选择器完整性与探测判断
    has_full_css = ptype == "css" and bool(parse.get("item")) and bool(parse.get("title"))
    need_detect = ptype == "css" and not has_full_css
    if need_detect and detected_parse:
        parse.update({k: v for k, v in detected_parse.items() if v})
        has_full_css = bool(parse.get("item")) and bool(parse.get("title"))
        need_detect = False
        stats["reused"] = True
    if need_detect and not auto_detect:
        print(f"\033[31m[⚠ 该站点已关闭AI自动探测，选择器为空，跳过：{src.get('name','?')}，不消耗积分]\033[0m")
        stats["skipped"] = True
        return [], stats
    # 翻页抓取
    for page in range(1, page_depth + 1):
        url = list_url if page == 1 else (page_pattern.replace("{page}", str(page)) if page_pattern else "")
        if not url:
            break
        try:
            html = fetch_with_requests(url, src.get("cookies"))
        except Exception as exc:  # noqa: BLE001
            items.append({"error": f"{url}: {exc}"})
            time.sleep(1.0 + random.random())
            continue
        # JS 动态页面检测：第一页即判断，动态直接跳过
        if page == 1 and is_dynamic_page(html):
            print(f"\033[31m[⚠ 该站点为JS动态渲染页面，静态爬虫无法解析，跳过：{src.get('name','?')}，不消耗AI积分]\033[0m")
            stats["dynamic"] = True
            stats["fail"] += 1
            return items, stats
        # 选择器为空：抓首页做启发式探测（不调 LLM，0 积分）
        if need_detect and page == 1:
            detected = auto_detect_selectors(html, list_url)
            stats["ai_calls"] += 1  # 计入一次"探测"动作（纯规则，0 积分）
            if detected:
                detected["type"] = "css"
                detected["link_prefix"] = site_origin or link_prefix
                parse.update({k: v for k, v in detected.items() if v})
                has_full_css = True
                need_detect = False
                stats["detected"] = True
                if root:
                    save_detected_selector(root, src.get("key", ""), detected)
                print(f"\033[33m[auto] 站点 {src.get('name','?')} 选择器为空，自动探测成功并回填："
                      f"item={detected.get('item','')} title={detected.get('title','')} "
                      f"date={detected.get('date','')} link={detected.get('link','')}\033[0m")
            else:
                print(f"\033[31m[auto] 站点 {src.get('name','?')} 自动探测未识别到有效列表结构，跳过，0 积分\033[0m")
                stats["fail"] += 1
                return items, stats
        try:
            if ptype == "css":
                items.extend(_parse_css(html, parse, link_prefix, default_stage, cutoff, list_url))
            elif ptype == "regex":
                items.extend(_parse_regex(html, parse, link_prefix, default_stage, cutoff, list_url))
            else:
                items.append({"error": f"{url}: 不支持的解析类型 {ptype}"})
                break
        except Exception as exc:  # noqa: BLE001
            items.append({"error": f"{url}: 解析失败 {exc}"})
        time.sleep(0.8 + random.random())
    stats["ok"] = sum(1 for it in items if "error" not in it)
    stats["fail"] = sum(1 for it in items if "error" in it)
    return items, stats


def _aes_decrypt_payload(data, key: str, iv: str, encoding: str = "base64"):
    """福建式响应解密：密文先按 encoding 解码，再 AES-128-CBC PKCS7 解密为 UTF-8 文本。"""
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except Exception:  # noqa: BLE001
        try:
            from Cryptodome.Cipher import AES
            from Cryptodome.Util.Padding import unpad
        except Exception:  # noqa: BLE001
            return None
    raw = base64.b64decode(data) if encoding == "base64" else bytes.fromhex(data)
    cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
    return unpad(cipher.decrypt(raw), 16).decode("utf-8", errors="ignore")


def _parse_api_json_custom(src, cutoff: str, end_date: str):
    """通用 JSON-API 源：POST JSON 请求，配置 api_params / api_page_size / 字段映射。
    兼容两种配置位置：顶层（旧）或 src.parse 内（admin 自助表单写入的 v2 结构）。
    扩展能力（webBuilder/esinteligentsearch 系列）：
      - api_form=True：以 form-urlencoded 提交（河南 getPageInfoListNewYzm）
      - api_list_field：点分路径定位列表字段（如 custom.infodata / result.records），
        中途遇到 JSON 字符串自动解析（陕西 esinteligentsearch content 为内层字符串）
      - api_page_field / api_size_field：自定义分页参数名（默认 page/size）
      - api_referer：自定义 Referer 头
      - api_sign_key：福建式 MD5 签名（portal-sign）
      - api_decrypt：响应 Data 字段 AES-CBC 解密，{key, iv, encoding}
      - api_page_start：起始页码（部分接口从 1 开始，如福建 PageNo）"""
    parse = src.get("parse") or {}
    api_url = src.get("api_url") or parse.get("api_url") or src.get("list_url")
    params = dict(src.get("api_params", {}) or parse.get("api_params", {}) or {})
    size = int(src.get("api_page_size", parse.get("api_page_size", 50)) or 50)
    title_f = src.get("title_field", parse.get("title_field", "title"))
    date_f = src.get("date_field", parse.get("date_field", "releaseTime"))
    link_f = src.get("link_field", parse.get("link_field", "linkTo"))
    prefix = src.get("link_prefix", "") or parse.get("link_prefix", "") or ""
    link_tpl = src.get("link_template", "") or parse.get("link_template", "") or ""
    stage = src.get("stage", "公告")
    list_field = src.get("api_list_field", "") or parse.get("api_list_field", "") or ""
    page_field = src.get("api_page_field", "") or parse.get("api_page_field", "") or "page"
    size_field = src.get("api_size_field", "") or parse.get("api_size_field", "") or "size"
    page_start = int(src.get("api_page_start", parse.get("api_page_start", 0)) or 0)
    page_calc = src.get("api_page_calc", "") or parse.get("api_page_calc", "") or ""
    time_field_name = src.get("api_time_field_name", "") or parse.get("api_time_field_name", "") or "infodate"
    form_mode = bool(src.get("api_form") or parse.get("api_form") or False)
    referer = src.get("api_referer", "") or parse.get("api_referer", "") or ""
    sign_key = src.get("api_sign_key", "") or parse.get("api_sign_key", "") or ""
    decrypt_cfg = src.get("api_decrypt", {}) or parse.get("api_decrypt", {}) or {}
    items = []
    for page in range(page_start, page_start + int(src.get("api_max_pages", parse.get("api_max_pages", 5)) or 5)):
        body = dict(params)
        page_val = page * size if page_calc == "multiply" else page
        body.update({page_field: page_val, size_field: size})
        date_time = bool(src.get("api_date_field_time") or parse.get("api_date_field_time") or False)
        if src.get("api_date_begin_field"):
            b = f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}"
            body[src["api_date_begin_field"]] = b + (" 00:00:00" if date_time else "")
        if src.get("api_date_end_field"):
            e = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
            body[src["api_date_end_field"]] = e + (" 23:59:59" if date_time else "")
        if src.get("api_time_field"):
            tf_start = f"{cutoff} 00:00:00" if not src.get("api_time_field_dash", parse.get("api_time_field_dash", False)) else f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]} 00:00:00"
            tf_end = f"{end_date} 23:59:59" if not src.get("api_time_field_dash", parse.get("api_time_field_dash", False)) else f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]} 23:59:59"
            body[src["api_time_field"]] = [{"fieldName": time_field_name,
                                            "startTime": tf_start,
                                            "endTime": tf_end}]
        try:
            data = fetch_api_json(api_url, body, form=form_mode, referer=referer, sign_key=sign_key)
        except Exception as exc:  # noqa: BLE001
            items.append({"error": f"{api_url}: {exc}"})
            break
        # 响应解密（福建式：Data 字段 base64+AES 后为 JSON 字符串）
        if decrypt_cfg and isinstance(data, dict):
            enc = data.get("Data") or data.get("data") or ""
            dec = _aes_decrypt_payload(enc, decrypt_cfg.get("key", ""), decrypt_cfg.get("iv", ""),
                                       decrypt_cfg.get("encoding", "base64"))
            if dec:
                try:
                    data = json.loads(dec)
                except Exception:  # noqa: BLE001
                    data = None
            else:
                data = None
        if data is None:
            break
        # 定位列表字段
        content = data
        if list_field:
            for part in list_field.split("."):
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except Exception:  # noqa: BLE001
                        content = None
                        break
                if isinstance(content, dict) and part in content:
                    content = content[part]
                else:
                    content = None
                    break
            if not isinstance(content, list):
                content = data
                for key in ("content", "list", "rows", "items", "data", "records"):
                    if isinstance(content, dict) and key in content:
                        content = content[key]
                        if isinstance(content, str):
                            try:
                                content = json.loads(content)
                            except Exception:  # noqa: BLE001
                                content = None
                                break
        else:
            for key in ("content", "list", "rows", "items", "data", "records"):
                if isinstance(content, dict) and key in content:
                    content = content[key]
        if isinstance(content, dict) and isinstance(content.get("content"), list):
            content = content["content"]
        if isinstance(content, dict) and isinstance(content.get("list"), list):
            content = content["list"]
        if not isinstance(content, list) or not content:
            break
        for it in content:
            if not isinstance(it, dict):
                continue
            title = str(it.get(title_f) or "").strip()
            if not title:
                continue
            date = str(it.get(date_f) or "")[:10]
            if src.get("api_date_field_ms") or parse.get("api_date_field_ms"):
                try:
                    ms = int(it.get(date_f) or 0)
                    date = (datetime.fromtimestamp(ms / 1000)).strftime("%Y-%m-%d")
                except Exception:  # noqa: BLE001
                    date = str(it.get(date_f) or "")[:10]
            href = str(it.get(link_f) or "")
            if link_tpl and not href:
                try:
                    d = dict(it)
                    tcode = str(it.get("noticeTypeCode") or "")
                    d["code"] = tcode.split("_")[-1] if "_" in tcode else ""
                    href = link_tpl.format(**d)
                except Exception:  # noqa: BLE001
                    href = ""
            href = abs_url(api_url, href, prefix)
            items.append({"title": title, "url": href, "date": date, "stage": stage})
        if len(content) < size:
            break
    return items


def _parse_css(html, parse, link_prefix, default_stage, cutoff, list_url=""):
    soup = BeautifulSoup(html, "lxml")
    item_sel = parse.get("item", "")
    nodes = soup.select(item_sel) if item_sel else soup.find_all(["li", "tr", "div"])
    out = []
    t_sel, d_sel, l_sel = parse.get("title", ""), parse.get("date", ""), parse.get("link", "")
    for node in nodes:
        t = node.select_one(t_sel) if t_sel else None
        if t is None:
            t = node.find("a")
        if t is None:
            continue
        title = t.get_text(strip=True)
        if not title or "{{" in title:
            continue
        d_node = node.select_one(d_sel) if d_sel else None
        d = d_node.get_text(strip=True) if d_node else node.get_text(" ", strip=True)
        date = _normalize_date(d)
        if date and date.replace("-", "") < cutoff:
            continue
        a = node.select_one(l_sel) if l_sel else t
        if a is None and l_sel and getattr(node, "name", "") == l_sel:
            a = node
        href = (a.get("href") or "") if a else ""
        href = abs_url(list_url, href, link_prefix)
        out.append({"title": title, "url": href, "date": date or "", "stage": default_stage})
    return out


def _parse_regex(html, parse, link_prefix, default_stage, cutoff, list_url=""):
    out = []
    pattern = parse.get("pattern", "") or ""
    if pattern:
        # 单 pattern + 命名组 (?P<title>...) (?P<date>...) (?P<link>...)
        rx = re.compile(pattern, re.S)
        for m in rx.finditer(html):
            title = (m.group("title") if "title" in m.re.groupindex else m.group(0) or "").strip()
            if not title:
                continue
            d = ""
            if "date" in m.re.groupindex:
                d = _normalize_date(m.group("date"))
            if d and d.replace("-", "") < cutoff:
                continue
            href = (m.group("link") if "link" in m.re.groupindex else "") or ""
            href = abs_url(list_url, href, link_prefix)
            out.append({"title": title, "url": href, "date": d, "stage": default_stage})
        return out
    # 旧式：三个独立正则 title/date/link
    title_re = re.compile(parse.get("title", ""), re.S)
    date_re = re.compile(parse.get("date", ""), re.S)
    link_re = re.compile(parse.get("link", ""), re.S)
    for m in title_re.finditer(html):
        title = m.group("t").strip()
        d = ""
        dm = date_re.search(html, m.start(), m.end() + 400)
        if dm:
            d = _normalize_date(dm.group("d"))
        if d and d.replace("-", "") < cutoff:
            continue
        href = ""
        lm = link_re.search(html, m.start(), m.end() + 400)
        if lm:
            href = lm.group("u")
        href = abs_url(list_url, href, link_prefix)
        out.append({"title": title, "url": href, "date": d, "stage": default_stage})
    return out


def _normalize_date(text):
    m = re.search(r"(\d{4})[-\/年.](\d{1,2})[-\/月.](\d{1,2})", text or "")
    if not m:
        return ""
    y, mo, da = m.groups()
    return f"{y}-{int(mo):02d}-{int(da):02d}"


def match_province(title: str):
    for prov, cities in PROVINCES.items():
        for token in cities:
            if token in title:
                return prov
    return None


def match_stage(title: str, rules=None):
    """按标题判断公告阶段（规划/招标/中标/更正/其他）。规则按优先级：更正 > 中标 > 规划 > 招标。"""
    for stage, words in (rules or STAGE_RULES):
        for w in words:
            if w in title:
                return stage
    return "其他"


def match_keywords(title: str, keywords=None):
    """按关键词组匹配，返回命中词列表；同义词组内任一词命中即收录。"""
    hits = []
    for group in (keywords or []):
        words = group.get("words", []) or []
        for w in words:
            if w and w in title and w not in hits:
                hits.append(w)
                break  # 每组只记一个代表词
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="回溯天数（覆盖配置，默认读配置）")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认脚本上级目录）")
    ap.add_argument("--only", default=None, help="只抓指定源（逗号分隔 key）")
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    root = args.out_dir or os.path.dirname(script_dir)
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)

    # 全局配置（settings + keywords + custom sources）
    pull_custom_config(root)
    settings, keywords, custom_sources = load_board_config(root)

    days = args.days if args.days else int(settings.get("date_range_days", 7) or 7)
    stages_selected = settings.get("stages") or []
    today = datetime.now()
    cutoff_date = today - timedelta(days=days)
    cutoff = cutoff_date.strftime("%Y%m%d")
    cutoff_dash = cutoff_date.strftime("%Y-%m-%d")
    end_dash = today.strftime("%Y-%m-%d")
    print(f"[settings] 日期范围: 近 {days} 天 | 阶段: {stages_selected if stages_selected else '全部'}")

    all_sources = SOURCES + custom_sources + [SZGGZY]
    only_keys = set(k.strip() for k in (args.only or "").split(",") if k.strip()) if args.only else None
    all_items = []
    errors = []
    auto_detect = bool(settings.get("auto_detect", True))
    total_ai_calls = 0
    total_detected = []
    total_reused = []
    total_dynamic = []
    total_skipped = []
    for src in all_sources:
        key = src.get("key")
        if only_keys and key not in only_keys:
            continue
        if key in ("ccgp", "csg"):
            parsed = parse_source(src, cutoff, days)
            stats = {"ok": sum(1 for e in parsed if "error" not in e),
                     "fail": sum(1 for e in parsed if "error" in e),
                     "dynamic": False, "ai_calls": 0, "detected": False,
                     "skipped": False, "reused": False}
        elif key == "szggzy":
            parsed = parse_szggzy(src, cutoff_dash, end_dash, days)
            stats = {"ok": sum(1 for e in parsed if "error" not in e),
                     "fail": sum(1 for e in parsed if "error" in e),
                     "dynamic": False, "ai_calls": 0, "detected": False,
                     "skipped": False, "reused": False}
        else:
            parsed, stats = parse_custom_source(src, cutoff, root, auto_detect)
            total_ai_calls += stats.get("ai_calls", 0)
            if stats.get("detected"):
                total_detected.append(src.get("name", key))
            if stats.get("reused"):
                total_reused.append(src.get("name", key))
            if stats.get("dynamic"):
                total_dynamic.append(src.get("name", key))
            if stats.get("skipped"):
                total_skipped.append(src.get("name", key))
        errs = [e for e in parsed if "error" in e]
        if errs:
            errors.extend(errs)
        parsed = [e for e in parsed if "error" not in e]
        # 采集日志增强：每条数据源结束打印 成功/失败 + 静态/动态标记
        kind_mark = "\033[31mJS动态-跳过\033[0m" if stats.get("dynamic") else "静态网页"
        flag = ""
        if stats.get("skipped"):
            flag = " [已关闭AI探测-跳过]"
        elif stats.get("detected"):
            flag = " [自动探测回填]"
        elif stats.get("reused"):
            flag = " [复用已存选择器]"
        print(f"[源] {src['name']}: 成功 {stats.get('ok', len(parsed))} 条 / 失败 {stats.get('fail', 0)} 条 | {kind_mark}{flag}")
        for it in parsed:
            it["source"] = key
            it["source_name"] = src["name"]
            it["source_kind"] = src["kind"]
            if not src.get("filter", True):
                it["_nofilter"] = True
                it["_src_province"] = src.get("source_province", "") or ""
        all_items.extend(parsed)
    print(f"[ai] 本次 AI 调用次数: {total_ai_calls}（纯规则探测，0 积分）"
          + (f" | 触发探测: {', '.join(total_detected)}" if total_detected else "")
          + (f" | 复用已存: {', '.join(total_reused)}" if total_reused else "")
          + (f" | 动态跳过: {', '.join(total_dynamic)}" if total_dynamic else "")
          + (f" | 开关跳过: {', '.join(total_skipped)}" if total_skipped else ""))

    # 去重（跨源同标题去重：不同源同步发布的同一公告只保留一条）
    seen = set()
    unique = []
    for it in all_items:
        key = (it["title"], it["date"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    # 过滤：省份 + 关键词 + 阶段多选
    filtered = []
    for it in unique:
        stage = match_stage(it["title"])
        it["stage"] = stage
        if stages_selected and stage not in stages_selected:
            continue
        if it.get("_nofilter"):
            it["province"] = it.get("_src_province", "") or ""
            it["matched_keywords"] = []
            filtered.append(it)
            continue
        prov = match_province(it["title"])
        if not prov:
            continue
        hits = match_keywords(it["title"], keywords)
        if not hits:
            continue
        it["province"] = prov
        it["matched_keywords"] = hits
        filtered.append(it)

    filtered.sort(key=lambda x: (x["date"], x["source"]), reverse=True)

    # ===== 累积合并：读取历史 data.json，与新抓取结果按 (title,date) 去重合并 =====
    # 每次抓取只覆盖窗口内数据，历史通过累积保留 → 回溯天数加大后看板能显示更久之前的数据
    data_path = os.path.join(data_dir, "data.json")
    prev_items = []
    prev_new_count = 0
    if os.path.exists(data_path):
        try:
            with open(data_path, encoding="utf-8") as f:
                prev_payload = json.load(f)
            prev_items = prev_payload.get("items", []) or []
            prev_new_count = prev_payload.get("new_count", 0) or 0
        except Exception:
            prev_items = []
    merged = {}
    for it in prev_items:
        if isinstance(it, dict) and it.get("title"):
            merged[(it["title"], it.get("date", ""))] = it
    fresh_keys = set()
    for it in filtered:
        merged[(it["title"], it.get("date", ""))] = it
        fresh_keys.add((it["title"], it.get("date", "")))
    # 排序（新的在前），超上限截断保留最近
    merged_items = sorted(merged.values(), key=lambda x: (x.get("date", ""), x.get("source", "")), reverse=True)
    if len(merged_items) > 20000:
        merged_items = merged_items[:20000]
    filtered = merged_items

    # ===== 已见公告库（去重）：历史抓取过的 (title,date) 不再算新增 =====
    seen_path = os.path.join(data_dir, "seen.json")
    seen_keys = set()
    if os.path.exists(seen_path):
        try:
            with open(seen_path, encoding="utf-8") as f:
                seen_keys = set(json.load(f).get("keys", []))
        except Exception:
            seen_keys = set()
    new_count = 0
    for it in filtered:
        k = (it["title"], it["date"])
        it["is_new"] = k in fresh_keys and k not in seen_keys
        if it["is_new"]:
            new_count += 1
    # 更新已见库（本次 key 全部并入，超上限截断保留最近）
    for it in filtered:
        seen_keys.add((it["title"], it["date"]))
    if len(seen_keys) > 50000:
        seen_keys = set(sorted(seen_keys)[-50000:])
    try:
        with open(seen_path, "w", encoding="utf-8") as f:
            json.dump({"updated": today.strftime("%Y-%m-%d %H:%M"),
                       "keys": sorted(seen_keys)}, f, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 写入 seen.json 失败: {exc}", file=sys.stderr)

    payload = {
        "generated_at": today.strftime("%Y-%m-%d %H:%M"),
        "window_days": days,
        "settings": {"date_range_days": days, "stages": stages_selected, "auto_detect": auto_detect},
        "total": len(filtered),
        "new_count": new_count,
        "ai_stats": {"ai_calls": total_ai_calls, "detected": total_detected,
                     "reused": total_reused, "dynamic": total_dynamic, "skipped": total_skipped},
        "items": filtered,
        "errors": errors[:30],
        "sources": [{"key": s["key"], "name": s["name"], "kind": s["kind"]} for s in all_sources],
    }

    data_path = os.path.join(data_dir, "data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    template_path = os.path.join(script_dir, "template.html")
    if not os.path.exists(template_path):
        print(f"[warn] 模板缺失: {template_path}", file=sys.stderr)
    else:
        with open(template_path, encoding="utf-8") as f:
            template = f.read()
        data_json = json.dumps(payload, ensure_ascii=False)
        data_json = data_json.replace("</", "<\\/")
        html_out = template.replace("__DATA__", data_json)
        with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
            f.write(html_out)

    print(json.dumps({
        "scanned": len(unique),
        "matched": len(filtered),
        "by_source": {},
        "data_file": data_path,
        "index_file": os.path.join(root, "index.html"),
        "errors": errors,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
