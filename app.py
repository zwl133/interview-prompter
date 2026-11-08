# -*- coding: utf-8 -*-
"""
面试提词器 · 桌面版 v0.2.7
单人自用 / 零依赖 / 本地运行

核心功能：听到问题 -> 2 秒内给出 3-5 条关键词要点（不给逐字稿）
素材来源：data/resume.md（你自己填的真实项目与数字）
可选：data/config.json 里配 API key 后走大模型，未配置则用本地检索引擎
v0.2.4：AI 走流式（首字 0.3~0.7s）+ 听到题就在后台预生成兜底答案
v0.2.6：技术题直接给「答-深-例」口语答案（不再说"我不会/回去补"）；连接预热消除首调 12s 冷启动
v0.2.7：技术题回车即自动作答，不再被不相干的素材顶掉（AI 挂了才退回素材）
"""

import ctypes
import json
import math
import os
import random
import re
import sys
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
SESSIONS = DATA / "sessions"
RESUME = DATA / "resume.md"
ANSWERS = DATA / "answers.md"
CONFIG = DATA / "config.json"
CONFIG_LOCAL = DATA / "config.local.json"   # 放 key 的本地文件，不进版本库

BG = "#12141a"
BG2 = "#191c24"
FG = "#e8ecf3"
DIM = "#8a93a6"
ACCENT = "#4da3ff"
WARN = "#ffb454"
GREEN = "#3ddc84"
RED = "#ff6b6b"
BAR_OFF = "#20242c"
BAR_DIM = "#1a2230"


def _blend(c1, c2, k):
    """两个 #rrggbb 按 k∈[0,1] 插值，用来做呼吸灯"""
    k = max(0.0, min(1.0, k))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))

# ---------------------------------------------------------------- 配置与素材

DEFAULT_CONFIG = {
    "ui": {"opacity": 0.95, "font_size": 13, "width": 540, "height": 470,
           "exclude_from_capture": True, "answer_mode": False},
    "llm": {"enabled": False, "base_url": "", "api_key": "", "model": "",
            "prefetch": True,          # 听到题就在后台把 AI 兜底生成好
            "rescue_max_tokens": 240},  # 兜底三行，技术题要容得下一句完整答案
    "asr": {
        "enabled": True,
        "model": "small",
        "mode": "loopback",          # loopback=系统声音（视频面试）  mic=麦克风（电话/线下）
        "silence_ms": 750,           # 静音多久算一句话说完（微信语音停顿长，600 会腰斩）
        "auto_cue": True,            # 识别到问题就自动弹提纲
        "hotwords": "以下是普通话面试问答。专有名词：Jenkins、JMeter、MySQL、Redis、Kafka、"
                    "Docker、Linux、Shell、Python、Selenium、Postman、TPS、QPS、ERP、BOM、SQL。",
    },
}


def load_config():
    if not CONFIG.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return DEFAULT_CONFIG
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_CONFIG
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict):
            merged.setdefault(k, {}).update(v)
        else:
            merged[k] = v

    # key 不进版本库：config.json 里留空，真实 key 放 config.local.json 或环境变量。
    # 优先级：环境变量 > config.local.json > config.json
    if CONFIG_LOCAL.exists():
        try:
            local = json.loads(CONFIG_LOCAL.read_text(encoding="utf-8"))
            for k, v in local.items():
                if isinstance(v, dict):
                    merged.setdefault(k, {}).update(v)
                else:
                    merged[k] = v
        except Exception:
            pass
    env_key = os.environ.get("INTERVIEW_LLM_KEY", "").strip()
    if env_key:
        merged.setdefault("llm", {})["api_key"] = env_key
    return merged


def load_materials():
    """把 resume.md 解析成素材条目：二级标题=分类，bullet=一条素材"""
    if not RESUME.exists():
        return []
    lines = RESUME.read_text(encoding="utf-8").splitlines()
    items, section = [], "通用"
    for ln in lines:
        s = ln.strip()
        if s.startswith("## "):
            section = s[3:].strip()
            continue
        if s.startswith("- ") and len(s) > 3:
            body = s[2:].strip()
            if body.startswith("(") or body.startswith("（"):  # 说明行跳过
                continue
            items.append({"section": section, "text": body})
    global _IDF
    _IDF = {}          # 素材变了，IDF 表必须重建
    return items


def load_answers():
    """把 answers.md 读成 {标题: 答案正文}；文件不存在就返回空"""
    if not ANSWERS.exists():
        return {}
    out, title, buf = {}, None, []

    def flush():
        if title and buf:
            out[title] = "\n".join(buf).strip()

    for ln in ANSWERS.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            flush()
            title, buf = None, []
        elif ln.startswith("### "):
            flush()
            title, buf = ln[4:].strip(), []
        elif title is not None:
            buf.append(ln)
    flush()
    return {k: v for k, v in out.items() if v}


def material_title(m):
    """素材条目 '【标题】正文' → 标题；取不到就返回 None"""
    hit = re.match(r"^[【\[](.*?)[】\]]", m["text"])
    return hit.group(1).strip() if hit else None


# ---------------------------------------------------------------- 问题分类

TYPES = [
    ("self_intro", ["自我介绍", "介绍一下自己", "介绍下自己", "简单介绍自己", "介绍自己", "介绍下你自己",
                    "背景", "毕业", "哪年", "学历", "几家公司", "工作经历", "履历", "自我评价"]),
    ("reverse", ["你有什么问题", "想问我", "反问", "有什么想了解"]),
    ("salary", ["薪资", "期望", "待遇", "package", "多少k", "年包"]),
    ("why_leave", ["离职", "为什么走", "跳槽", "换工作", "离开"]),
    ("weakness", ["缺点", "不足", "短板", "不好的地方"]),
    ("strength", ["优点", "优势", "长处", "过人"]),
    ("failure", ["失败", "没做好", "遗憾", "后悔", "错误"]),
    ("bug", ["印象比较深", "印象最深", "什么bug", "哪些bug", "bug定位", "定位不到", "踩过的"]),
    ("practice", ["代码评审", "代码review", "code review", "质量内建", "测试左移",
                  "漏测", "用例评审", "测试流程", "准入", "准出", "封版", "上线流程", "测试规范"]),
    ("teamwork", ["分工", "团队规模", "团队多少人", "几个人", "团队组成"]),
    ("challenge", ["难点", "挑战", "困难", "踩坑", "故障", "棘手"]),
    ("team", ["协作", "冲突", "分歧", "同事", "沟通", "团队", "开发说", "不认为是",
              "推不动", "驳回", "扯皮"]),
    ("plan", ["规划", "职业发展", "三年", "五年", "未来"]),
    ("behavior", ["举例", "有没有一次", "讲一个", "说一个", "举个例子", "当时"]),
    ("project", ["项目", "做的", "负责", "经历", "系统", "业务"]),
]

FRAMEWORKS = {
    "self_intro": ("自我介绍", "60–90 秒", [
        "一句话定位：几年 + 什么方向 + 一句最强标签",
        "挑 2 段最贴岗位的经历，各 30 秒，只讲动作和数字",
        "收尾：为什么是这个岗位 / 这家公司",
    ]),
    "project": ("项目深挖", "90 秒", [
        "结论先行：一句话说清项目是什么、你负责哪块",
        "你的动作：挑一条素材，讲一个具体决策",
        "取舍：为什么这么选，放弃了什么",
        "结果：量化数字 + 一句「如果重来会…」",
    ]),
    "challenge": ("难点/故障", "90 秒", [
        "先定义难在哪（一句话，别铺背景）",
        "排查路径：怎么定位的，用了什么手段",
        "关键判断：当时为什么这么赌",
        "结果 + 沉淀：留下了什么机制/工具",
    ]),
    "failure": ("失败复盘", "60 秒", [
        "选一个真失败、但不致命的案例",
        "我的责任在哪（别甩锅给外部）",
        "当时怎么补救的",
        "之后改了什么习惯/机制（必须有）",
    ]),
    "why_leave": ("离职原因", "40 秒", [
        "对事不对人：说发展/方向，不说人际关系",
        "给出可验证的客观理由",
        "接一句：为什么下一站是这里（岗位匹配）",
    ]),
    "salary": ("薪资期望", "30 秒", [
        "先给区间，不给单点（区间宽度 15–20%）",
        "依据：当前包 + 市场行情 + 岗位职责",
        "留口子：整体 package 可谈，更看重成长",
    ]),
    "team": ("协作/冲突", "60 秒", [
        "冲突是什么（立场差异，不是人品问题）",
        "我怎么推进的：找共同目标 / 用数据说话",
        "结果 + 关系维护",
    ]),
    "strength": ("优势", "40 秒", [
        "选 1 个最贴岗位的优势，别报三个",
        "一个证据（项目/数字）",
        "落到岗位价值上",
    ]),
    "weakness": ("短板", "40 秒", [
        "选一个真实但不致命的",
        "正在怎么补（具体动作，不是「我在努力」）",
        "补到什么程度了",
    ]),
    "plan": ("职业规划", "40 秒", [
        "1 年：把当前岗位做扎实（具体能力）",
        "3 年：能独立负责一条线/一个方向",
        "别说得太远，别说要创业",
    ]),
    "behavior": ("行为题", "60 秒", [
        "STAR 压缩成三句：情境一句 / 动作两句 / 结果一句带数字",
        "只讲一个案例，别串三个",
        "结尾给一句反思",
    ]),
    "reverse": ("反问", "20 秒", [
        "问团队现在最痛的一件事",
        "问这个岗位 6 个月的成功标准",
        "别先问薪资和加班",
    ]),
    "bug": ("印象深的 bug", "60–90 秒", [
        "先一句话说现象：哪个模块、什么表现",
        "怎么发现的：用了什么手段（并发/边界/异常流）",
        "怎么定位：日志→接口返回→库表→代码",
        "根因一句话（例：并发下唯一校验来不及判断）",
        "修复方式 + 后续怎么防（加锁/加校验/加用例）",
    ]),
    "teamwork": ("团队分工", "40 秒", [
        "总人数 + 角色拆分（产品/UI/前端/后端/测试）",
        "我负责哪块：具体模块 + 边界",
        "其他人各一句（组长管什么、实习生管什么）",
        "协作方式：需求对齐 → 提测 → 进度同步",
    ]),
    "practice": ("测试实践", "60 秒", [
        "先给一句判断，不要背定义——说你的观点",
        "接一个你项目里的具体做法，最好带数字",
        "说清这么做的代价或它的边界，显得你想过而不只是做过",
    ]),
    "other": ("没准备过的题", "40 秒", [
        "先接住：「这个我先说下我的理解，您看准不准」——顺手复述题目，确认没听错",
        "划边界：「这块我没有直接做过，不敢乱说」——比硬编安全",
        "迁移：把项目里最接近的一件事搬过来，讲清哪像、哪不像",
        "收口：「具体到这个，我的理解是…，我打算…」——别硬撑过 40 秒",
    ]),
}

STOP_CHARS = set("的了是我你他她它们在有和与就都也很吗呢吧啊这个那个什么怎么为什么如何可以能否一下请问您")


def classify(q):
    # 先去掉「你/您」这类称呼，避免「介绍一下你自己」匹配不上
    qn = q.replace("你", "").replace("您", "")
    best, best_hits, best_len = "other", 0, 0
    for key, kws in TYPES:
        hits = 0
        total = 0
        for k in kws:
            if k in qn:
                hits += 1
                total = max(total, len(k))
        # 命中数优先，同分时更长的关键词（更具体）胜出
        if hits > best_hits or (hits == best_hits and hits > 0 and total > best_len):
            best, best_hits, best_len = key, hits, total
    return best


# 题型 -> 优先取用的素材分类（靠前的加分更多）
SECTION_PREF = {
    "self_intro": ["个人与职业（HR 类）", "项目 · ERP 整体"],
    "project": ["项目 · ERP 整体", "项目 · 测试实践"],
    "challenge": ["项目 · ERP 整体", "项目 · 测试实践"],
    "failure": ["项目 · 测试实践", "项目 · ERP 整体"],
    "behavior": ["项目 · 测试实践", "项目 · ERP 整体"],
    "team": ["项目 · 测试实践", "个人与职业（HR 类）", "项目 · ERP 整体"],
    "practice": ["项目 · 测试实践", "测试设计与方法"],
    "strength": ["个人与职业（HR 类）", "可量化成果（数字速查）"],
    "weakness": ["个人与职业（HR 类）"],
    "plan": ["个人与职业（HR 类）"],
    "salary": ["可量化成果（数字速查）", "个人与职业（HR 类）"],
    "why_leave": ["个人与职业（HR 类）"],
    "reverse": ["个人与职业（HR 类）"],
    "bug": ["项目 · 测试实践"],
    "teamwork": ["项目 · ERP 整体"],
    "other": [],
}

# 主题路由：问题里出现这些词，直接锁定对应分区（解决「邻居条目」误命中）
TOPIC_RULES = [
    ("网络基础", ["tcp", "udp", "osi", "七层", "四层", "http", "https", "dns", "url", "三次握手",
                  "状态码", "cookie", "session", "token", "get 和 post", "get和post"]),
    ("MySQL", ["mysql", "sql", "索引", "慢查询", "explain", "存储过程", "连表", "内连接", "左连接",
               "右连接", "增删改查", "delete", "truncate", "drop", "调优", "数据库", "varchar"]),
    ("性能测试", ["性能", "并发", "qps", "tps", "吞吐量", "jmeter", "响应时间", "压测", "3-5-8"]),
    ("接口测试", ["接口", "越权", "幂等", "鉴权", "加密解密", "restful", "api"]),
    ("自动化测试", ["自动化", "selenium", "pytest", "框架", "allure", "元素定位", "装饰器",
                    "关键字驱动", "kdt", "用例数", "覆盖率", "验证码"]),
    ("Python", ["python", "列表", "元组", "字典", "去重", "反转", "深拷贝", "浅拷贝", "冒泡", "数据类型"]),
    ("Linux 与 Shell", ["linux", "shell", "日志", "tail", "crontab", "定时任务", "服务器", "ifconfig",
                        "top", "free", "df", "部署", "pid"]),
    ("Jenkins 与 Docker", ["jenkins", "docker", "ci", "持续集成", "持续部署", "镜像", "容器"]),
    ("测试设计与方法", ["测试用例", "用例", "设计方法", "场景法", "等价类", "边界值", "判定表",
                        "不漏测", "冒烟", "回归", "bug 定位", "怎么测", "测试点", "风险"]),
]


# 同义词扩展：问题里的抽象词，扩展到素材里可能用的具体词
SYNONYMS = {
    "挑战": ["复杂", "难点", "困难", "印象深刻"],
    "难点": ["复杂", "困难", "棘手"],
    "最复杂": ["难点", "挑战"],
    "困难": ["难点", "踩坑", "印象深刻"],
    "提升效率": ["提效", "自动化"],
    "效率": ["提效", "自动化"],
    "遇到困难": ["踩坑", "故障", "难点"],
}


def _expand(q):
    extra = []
    for k, syns in SYNONYMS.items():
        if k in q:
            extra.extend(syns)
    return q + "".join(extra) if extra else q


def route_topic(q):
    """返回命中的分区名（取命中次数最多的），没有命中返回 None"""
    ql = q.lower().replace(" ", "")
    best, best_hits = None, 0
    for section, kws in TOPIC_RULES:
        hits = sum(1 for k in kws if k.replace(" ", "") in ql)
        if hits > best_hits:
            best, best_hits = section, hits
    return best


def _ngrams(text, n=2):
    t = re.sub(r"[\s，。？！、；：「」（）()\[\]【】]", "", text)
    return {t[i:i + n] for i in range(len(t) - n + 1)}


# 面试官一句话常带三四个分句，真正的提问多埋在最后；语气词和客套会把信号稀释掉
SPLIT_RE = re.compile(r"[。！？；;!?，,、\n]+")
LEAD_WORDS = ("那么", "那", "嗯", "呃", "行", "好", "然后", "另外", "还有", "所以", "其实")
FILLERS = ("嗯", "啊", "呃", "哦", "那个", "这个", "就是", "然后", "那么", "其实",
           "大概", "简单", "具体", "一下", "请问", "我们", "咱们", "先", "聊聊",
           "聊一聊", "说说", "讲讲", "谈谈", "了解", "刚才", "提到", "你说的",
           "你说", "您说的", "您说", "你先", "这样")


def _strip_fillers(s):
    for w in sorted(FILLERS, key=len, reverse=True):
        s = s.replace(w, "")
    return s.strip()


def _split_query(q):
    parts = []
    for p in SPLIT_RE.split(q or ""):
        p = p.strip()
        changed = True
        while changed:
            changed = False
            for w in LEAD_WORDS:
                if p.startswith(w) and len(p) - len(w) >= 3:
                    p = p[len(w):].strip("，, ")
                    changed = True
        if len(p) >= 2:
            parts.append(p)
    return parts or ([q] if q else [])


# 二元组 IDF：素材库里遍地是的词（「测试」「项目」）权重低，稀有的词（「幂等」「越权」）权重高
_IDF = {}


def _build_idf(materials):
    global _IDF
    df = {}
    for m in materials:
        for g in _ngrams(m["text"]):
            df[g] = df.get(g, 0) + 1
    n = max(1, len(materials))
    _IDF = {g: math.log((n + 1) / (c + 0.5)) + 0.6 for g, c in df.items()}


def _name_coverage(name, q):
    """标题里的字有多大比例出现在问题里（容忍空格与语气词差异）"""
    chars = {c for c in name if c not in STOP_CHARS and not c.isspace()}
    if not chars:
        return 0.0
    return len(chars & set(q)) / len(chars)


def _lcs_len(a, b):
    """最长公共子串长度：问题里原样带着标题片段，是最强的匹配信号"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _score_one(seg, m, kind, topic, prefs):
    """单个子句对单条素材的相关度"""
    t = m["text"]
    grams_t = m.get("_grams")
    if grams_t is None:
        grams_t = m["_grams"] = _ngrams(t)
    variants = [seg]
    clean = _strip_fillers(seg)
    if clean and clean != seg:
        variants.append(clean)
    seg_grams = set()
    for s in variants:
        seg_grams |= _ngrams(_expand(s), 2)

    hit = seg_grams & grams_t
    if not hit:
        return 0.0
    # 稀有词命中一次比常见词命中三次更有价值；再按素材长度衰减，避免长条目通吃
    bi = sum(_IDF.get(g, 1.8) for g in hit) / (len(grams_t) ** 0.35)

    chars = {c for c in "".join(variants) if c not in STOP_CHARS and not c.isspace()}
    overlap = len(chars & set(t)) / (len(set(t)) ** 0.5)

    sec_bonus = 0.0
    if m["section"] in prefs:
        sec_bonus = 3.0 - 0.9 * prefs.index(m["section"])
    topic_bonus = 6.0 if (topic and m["section"] == topic) else 0.0

    name = re.match(r"^[【\[]([^】\]]+)[】\]]", t)
    if name:
        nm = name.group(1)
        cov = max(_name_coverage(nm, s) for s in variants)
        lcs = max(_lcs_len(nm, s) for s in variants)
        name_bonus = 5.0 * cov + 1.6 * lcs
    else:
        name_bonus = 0.0

    digits = len(re.findall(r"\d", t))
    return bi + overlap * 0.3 + sec_bonus + topic_bonus + name_bonus + min(digits, 6) * 0.15


def score_materials(q, materials, kind="other", topn=2, threshold=0.8):
    """子句分别打分后取最大值：长问题不会再把短而准的信号冲淡"""
    if not materials:
        return []
    if not _IDF:
        _build_idf(materials)
    segs = _split_query(q) or [q]
    prefs = SECTION_PREF.get(kind, [])
    whole_topic = route_topic(q)
    best = {}
    for idx, seg in enumerate(segs):
        topic = route_topic(seg) or whole_topic
        weight = 1.0 + 0.10 * idx          # 越靠后的分句通常才是真问题
        for m in materials:
            s = _score_one(seg, m, kind, topic, prefs) * weight
            old = best.get(m["text"])
            if old is None or s > old[0]:
                best[m["text"]] = (s, m)
    ranked = sorted(best.values(), key=lambda x: -x[0])
    if not ranked or ranked[0][0] <= threshold:
        return []
    # 相对门槛：和最高分差距过大的不给，避免凑满两条噪音
    cutoff = max(threshold, ranked[0][0] * 0.5)
    return [m for s, m in ranked[:topn] if s >= cutoff]


# 题型兜底：这类问题的答案永远是"同一个故事"，字面匹配不到就直接给锚点素材
ANCHOR_SECTION = {
    "self_intro": ("个人与职业（HR 类）",),
    "project": ("项目 · ERP 整体", "项目 · 测试实践"),
    "challenge": ("项目 · ERP 整体", "项目 · 测试实践"),
    "behavior": ("项目 · ERP 整体", "项目 · 测试实践"),
    "failure": ("项目 · ERP 整体", "个人与职业（HR 类）"),
    "teamwork": ("项目 · ERP 整体", "个人与职业（HR 类）"),
    "bug": ("项目 · 测试实践", "项目 · ERP 整体"),
    "practice": ("项目 · 测试实践", "测试设计与方法"),
}


def _anchor(materials, key):
    for sec in ANCHOR_SECTION.get(key, ()):
        for m in materials:
            if m["section"] == sec:
                return m
    return None


# 这两类问题的答案基本固定，标题能定位就直接给，避免被"上一家公司"这类字面重合带跑
PINNED = {"self_intro": "自我介绍", "project": "项目介绍"}


GENERIC_Q = re.compile(
    "怎么理解|怎么看待|怎么设计|怎么实现|怎么保证|怎么做|怎么测|怎么用|怎么办|怎么样|怎样|"
    "如何看待|你怎么看|你觉得|你认为|你认为呢|你们|我们|用过吗|用过|"
    "介绍一下|说说|谈谈|讲讲|聊聊|什么是|是什么|怎么样了|怎么做|怎么测|怎么用|怎么办|"
    "如何|为什么|有哪些|有没有|区别|不同|一下|请问|能不能|可不可以|吗|呢|吧|啊")


def _content_text(text):
    """去掉提问套话后的"实义文本"——'怎么理解质量内建' → '质量内建'"""
    return GENERIC_Q.sub("", text or "")


def _content_chars(text):
    return {c for c in _content_text(text)
            if not c.isspace() and c not in STOP_CHARS}


def _pick_quality(q, m):
    """素材标题与问题的贴合度：只算实义字，避免'怎么理解/你们'这类套话刷分"""
    name = re.match(r"^[【\[]([^】\]]+)[】\]]", m["text"])
    if not name:
        return 0.0
    nm = _content_text(name.group(1))
    tn = _content_chars(nm)
    if not tn:
        return 0.0
    best = 0.0
    for s in [q] + _split_query(q):
        tq = _content_chars(s)
        cov = len(tq & tn) / len(tn)
        lcs = _lcs_len(nm, _content_text(_strip_fillers(s)))
        best = max(best, cov, min(1.0, lcs / 4.0))
    return best


COVER_HIT = 0.6
COVER_NEAR = 0.25


def assess_coverage(question, picks, pinned=None):
    """这道题准备得怎么样：hit=能直接用 / near=只有接近的 / miss=库里没有"""
    if not picks:
        return "miss", 0.0
    best = max(_pick_quality(question, m) for m in picks)
    if pinned is not None and picks and picks[0] is pinned:
        return "hit", max(best, COVER_HIT)
    if best >= COVER_HIT:
        return "hit", best
    if best >= COVER_NEAR:
        return "near", best
    return "miss", best


def build_rescue_cue(picks, anchor=None):
    """题库里没有这道题：给能照着做的接话结构，而不是空框架"""
    out = ["▍ 这题没准备过 ｜ 别硬编，按这个接", ""]
    out.append("① 先接住：「这个我先说下我的理解，您看准不准」")
    out.append("   └ 顺手复述一遍题目——确认没听错，也给自己 5 秒")
    out.append("② 划边界：「这块我没有直接做过，不敢乱说」")
    out.append("   └ 硬编一句，被追问一层就崩，比承认更亏")
    out.append("③ 迁移：把项目里最接近的一件事搬过来，说清哪像、哪不像")
    near = picks[0] if picks else anchor
    if near:
        t = material_title(near) or near["text"][:12]
        out.append("   └ 库里最近：%s（概念不同，只借经历，别当答案念）" % t)
    out.append("④ 收口：「具体到这个，我的理解是…，我打算…」")
    out.append("")
    out.append("⏱ 40 秒内收住｜面试官看的是你怎么面对不会的，不是你会不会")
    return "\n".join(out)


def build_local_cue(question, materials):
    key = classify(question)
    title, dur, points = FRAMEWORKS[key]
    # 反问环节不需要调素材
    picks = [] if key == "reverse" else score_materials(question, materials, key)
    strong = [m for m in picks if _pick_quality(question, m) >= 0.45]
    kw = PINNED.get(key)
    pin = None
    if kw:
        pin = next((m for m in materials if kw in m["text"][:14]), None)
    if pin:
        picks = [pin] + [m for m in strong if m is not pin][:1]
    elif strong:
        picks = strong
    # 长句/口语化提问常常字面匹配不到，但答案就是固定那一段：直接给锚点素材
    anchor = None
    if not picks and key != "reverse":
        anchor = _anchor(materials, key)
        if anchor:
            picks = [anchor]

    level, score = assess_coverage(question, picks, pin)
    if key == "reverse":
        level = "hit"          # 反问环节不需要素材，本地就够用

    # 库里确实没有 → 给「怎么接」的结构，不要给空框架骗自己
    if level == "miss" and key != "reverse":
        return build_rescue_cue(picks, anchor), key, picks, level

    near = (level == "near")

    # 技术题（网络/数据库/性能/接口/自动化…）直接给答案要点，不套通用框架
    # 未识别题型但素材确实命中的，同理——有料就给料，别套"没准备过"的框架
    topic = route_topic(question)
    if picks and (topic or (key == "other" and level == "hit")):
        if topic:
            head = "▍ %s ｜ %s" % (topic, "接近，说清差别" if near else "直接答")
        else:
            head = "▍ 命中素材 ｜ %s" % ("接近，说清差别" if near else "直接答")
        out = [head, ""]
        for m in picks:
            out.append("· " + m["text"])
            out.append("")
        if "优缺点" in question:
            out.append("（先讲一个真优势，再讲一个不致命的短板，短板后跟一句「我在怎么补」）")
        return "\n".join(out).rstrip(), key, picks, level

    out = ["▍ %s ｜ 建议 %s" % (title, dur), ""]
    marks = "①②③④⑤"
    for i, p in enumerate(points):
        out.append("%s %s" % (marks[i % 5], p))
    out.append("")

    if picks:
        if near:
            out.append("⚠ 只有接近的素材（说清哪里不一样，别当原题答案念）")
        else:
            out.append("💡 可用素材（照你自己的话说，别念）")
        for m in picks:
            out.append("· " + m["text"])
    else:
        out.append("💡 素材库里没命中相关经历 —— 先补 data/resume.md")
    if "优缺点" in question:
        out.append("（被问到「优缺点」：先讲一个真优势，再讲一个不致命的短板，短板后面必须跟一句「我在怎么补」）")
    return "\n".join(out), key, picks, level


def _split_sentences(text):
    """按句号/问号/感叹号切成一行一句，方便扫读"""
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？!?":
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return parts


ANSWER_MIN = 0.40      # 草稿也要够像这道题才给：0 分的素材照念只会更糟


def build_answer(question, answers, picks):
    """答案模式：给出预生成的回答草稿。→ (文本, 命中标题)；没命中返回 (None, None)

    这里必须卡分数：检索在完全不相关时会顺手塞两条垫底素材，原来拿第一条有草稿的
    就返回，于是问「你最大的缺点是什么」能念出「shell 里 $3 是什么」的答案。
    """
    for m in picks:
        t = material_title(m)
        if t and t in answers and _pick_quality(question, m) >= ANSWER_MIN:
            out = ["▍ 回答草稿 ｜ 顺着说，别念", ""]
            out += _split_sentences(answers[t])
            out += ["", "—— 【%s】｜改过的句子才是你的" % t]
            return "\n".join(out), t
    return None, None


# ---------------------------------------------------------------- 大模型（可选）

def _llm_url(llm):
    url = llm["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def _chat(llm, sys_p, user, max_tokens=500, timeout=20, temperature=0.3):
    """OpenAI 兼容接口的最小调用，失败返回 None（调用方一律要能降级）"""
    body = json.dumps({
        "model": llm.get("model") or "deepseek-chat",
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(_llm_url(llm), data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + llm["api_key"],
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _chat_stream(llm, sys_p, user, on_delta=None, max_tokens=240,
                 timeout=40, temperature=0.4):
    """流式调用：每收到一小段就回调 on_delta(chunk)，首字通常 0.3-0.7 秒。

    体感差距全在这里——非流式要等整段生成完才返回，流式第一个「接：」先出口，
    后面的字边生成边显示，用户不用干等。失败也返回已收到的部分，方便降级。
    """
    body = json.dumps({
        "model": llm.get("model") or "deepseek-chat",
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(_llm_url(llm), data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + llm["api_key"],
    })
    got = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line or not line.startswith("data:"):
                    continue          # 空行 / 心跳
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    d = json.loads(payload)
                except Exception:
                    continue
                ch = (d.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                if ch:
                    got.append(ch)
                    if on_delta:
                        try:
                            on_delta(ch)
                        except Exception:
                            pass
    except Exception:
        pass
    return "".join(got).strip() or None


RESCUE_PROMPT = """你在给一个正在视频面试的人写提词。他只有十秒扫一眼就要开口，所以你写的是"能直接说出口的话"，不是提纲也不是讲义。

先看【题类】，只用对应那一种格式：

一、技术题（问概念、原理、区别、怎么优化、怎么实现）
你比他懂，直接把答案讲出来。三行：
答：<一句话正面回答，40 字内。开口就是结论或定义，不要"这个问题呢"式铺垫>
深：<再往下走一层：为什么这样、内部怎么走、或者具体怎么做，60 字内>
例：<落到实际一句。素材里有相关经历就带素材里的真实细节；没有就讲工程上常见的做法，别说"我在学/刚接触">
技术题绝对禁止出现：我不会、没做过、没实操过、没接触过、不太熟、不熟悉、了解不深、回去补、后续学习、这块经验比较少、我在学。问了就得答出来。

二、经历题（问你的项目、难点、离职原因、团队冲突、职业规划）
素材里有就照着说；素材里确实没有，才用三行：
接：<一句话把话头接住，顺手把题目复述一遍>
移：<挑素材里最近的一条，说具体做法，点一句哪里不一样>
收：<用你真做过的事收住，可以加一句"这块我还在攒经验">
收不许说"回去补""后续学习"，也不许装懂编造。

两种情况都遵守：
1) 讲"我自己的事"只能用素材里的事实，不编项目名、公司、数字、工具名；
2) 全口语，像跟同事当面解释，可以用"简单说就是""说白了""举个例子"；术语第一次出现就用一句人话解释清楚，别堆名词；
3) 不用"首先/其次/综上所述"，不写序号、标题、括号注释，不解释你在做什么；
4) 三行封顶，行间不空行，总数不超过 200 字。写完立刻停。"""

# 技术题里这些是"认输话术"，出现就说明模型没在答题——本地再削一遍
HEDGE_RE = re.compile(
    "我不会|不太会|没做过|没实操过|没接触过|没写过|没深入了解|了解不深|不太熟|不熟悉|"
    "经验比较少|经验不多|这块比较薄弱|回去补|回去再|后续补|继续学习|补一下|"
    "没怎么用过|不太了解|确实不了解|我在学|正在学|刚开始学|刚接触|还在学")


def _strip_hedge(text):
    """技术题答案里剔除"我不会/回去补"这类句子，保留真正讲内容的部分。

    提示词已经禁了，但模型偶尔还是会漏一句。这里按句切，命中话术的整句丢掉，
    行首的「答：」「深：」「例：」保留；整行都被削空就丢掉这一行。
    """
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        pre, body = "", ln.strip()
        m = re.match(r"^([答深例接移收][：:]\s*)", body)
        if m:
            pre, body = m.group(1), body[m.end():]
        kept = [p for p in re.split(r"(?<=[。！？；])", body) if not HEDGE_RE.search(p)]
        s = "".join(kept).strip(" ，,、。")
        s = re.sub(r"^(但|但是|不过|所以|而且)[，,]?", "", s).strip()
        if s:
            out.append(pre + s)
    return "\n".join(out)


def _mats_for_llm(materials, picks=None, limit=12):
    """优先用检索命中的素材：更相关，也把 prompt 从 18 条压到 12 条，首字更快"""
    src = [m for m in (picks or []) if m] or []
    seen = {id(m) for m in src}
    for m in materials:
        if len(src) >= limit:
            break
        if id(m) not in seen:
            src.append(m)
            seen.add(id(m))
    return "\n".join("- " + m["text"] for m in src[:limit]) or "（无）"


def llm_warmup(cfg):
    """用一次极小的请求把连接建好。

    实测：新进程里的第一枪首字要 12 秒（DNS/TLS + 上游冷启动），然后再怎样都是
    0.4~1s。提前空放一枪（0.2s 就返回），面试正式开始时就碰不到这个坑。
    """
    llm = cfg.get("llm", {})
    if not (llm.get("api_key") and llm.get("base_url")):
        return
    body = json.dumps({
        "model": llm.get("model") or "deepseek-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1, "stream": True,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(_llm_url(llm), data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + llm["api_key"],
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            for raw in r:
                if raw.strip().startswith(b"data:"):
                    break
    except Exception:
        pass


# 技术题判据：光靠题型分类不够——「怎么设计一个秒杀系统」会被 "系统" 撞成 project，
# 但它其实是纯技术题，该直接给答案。这里按问法再判一道。
TECH_Q = re.compile(
    "什么是|是什么|啥是|区别|差异|原理|底层|机制|为什么|怎么优化|如何优化|怎么实现|如何实现|"
    "怎么设计|如何设计|怎么保证|如何保证|怎么排查|怎么用|如何用|会不会|了解|对比|设计一个|"
    "性能|调优|并发|一致性|索引|缓存|事务|幂等")
PERSONAL_Q = re.compile(
    "你做过|你负责|你的项目|你们项目|你的经历|讲讲你|说一个你|举例|举个例子|"
    "上一家|离职|跳槽|你的团队|你个人|你遇到|你踩|你的规划|你的缺点|你的优点|"
    "换工作|为什么想|为什么选择|为什么来|为什么投|我们公司|加班|薪资|待遇|到岗|入职|"
    "缺点|优点|优势|短板|不足|性格|职业规划|三年|五年|你个人")


def is_technical(q):
    """这题该"直接给答案"还是"讲自己的经历"？"""
    if PERSONAL_Q.search(q):
        return False
    return bool(TECH_Q.search(q)) or classify(q) == "other"


def llm_rescue(question, materials, cfg, picks=None, on_delta=None, kind=""):
    """题库外的题：技术题直接给口语答案，经历题给「接-移-收」。没配 key 返回 None"""
    llm = cfg.get("llm", {})
    if not (llm.get("api_key") and llm.get("base_url")):
        return None
    technical = is_technical(question)
    tag = "技术题｜必须直接给出答案，不许说不会" if technical else "经历题｜能对上素材就照着说"
    user = "【题类】%s\n【我的素材】\n%s\n\n【面试官问】\n%s" % (
        tag, _mats_for_llm(materials, picks), question)

    def _push(ch):
        if on_delta:
            on_delta(ch)

    txt = _chat_stream(llm, RESCUE_PROMPT, user, on_delta=_push,
                       max_tokens=int(llm.get("rescue_max_tokens", 240)),
                       timeout=40, temperature=0.45)
    if txt and technical:
        txt = _strip_hedge(txt) or txt
    return txt


def llm_cue(question, materials, cfg, picks=None, on_delta=None):
    llm = cfg.get("llm", {})
    if not (llm.get("enabled") and llm.get("api_key") and llm.get("base_url")):
        return None
    sys_p = (
        "你是面试提词器。输出的是提词卡，不是稿子。\n"
        "硬规则：1) 只输出 3-5 条要点，每条不超过 22 个汉字；"
        "2) 写成短语/关键词，不要完整句子，不要「首先其次」；"
        "3) 必须引用素材里的真实项目名或数字；"
        "4) 最后一行用「💡 」开头给 1-2 条最相关素材原文；"
        "5) 不要解释你在做什么，直接输出内容。"
    )
    user = "【我的素材】\n%s\n\n【面试官问】\n%s" % (_mats_for_llm(materials, picks, 18), question)
    return _chat_stream(llm, sys_p, user, on_delta=on_delta, max_tokens=320,
                        timeout=40, temperature=0.3)


# ---------------------------------------------------------------- 后台预热

class Live:
    """一块正在流式生成的内容：工作线程往里写，主线程只读，靠 60ms 的 tick 刷新"""

    __slots__ = ("kind", "q", "head", "text", "state", "t0", "ttft", "err", "drawn",
                 "logged", "slow")

    def __init__(self, kind, q, head=""):
        self.kind = kind          # "rescue" | "cue"
        self.q = q
        self.head = head
        self.text = ""
        self.state = "run"        # run / done / fail
        self.t0 = time.time()
        self.ttft = None
        self.err = ""
        self.drawn = None         # 上次画到哪，避免重复重绘
        self.logged = False
        self.slow = False         # 慢到该提醒用户别干等了

    def push(self, ch):
        """工作线程收到一小段就追加；首段到达时间记下来，用于报「首字 0.4s」"""
        if not ch:
            return
        if self.ttft is None:
            self.ttft = time.time() - self.t0
        self.text += ch

    def elapsed(self):
        return time.time() - self.t0


class Prefetch:
    """ASR 一拿到问题就在后台把「接-移-收」生成好，等你按 ^Enter 时结果已经在手里。

    同一个问题只跑一次；最多留 6 条，旧的随手扔掉。花的是 token，省的是现场的几秒。
    """

    KEEP = 6

    def __init__(self):
        self._slots = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(q):
        """只按实义字符去重：「你怎么理解 压测？」和「你怎么理解压测」算同一题"""
        return re.sub(r"[\s，。？！、,.?!：:；;“”\"'（）()~·…—-]", "", q or "")

    def get(self, q):
        with self._lock:
            return self._slots.get(self.key(q))

    def find(self, q):
        """先精确找，找不到就找最像的那条——ASR 常把一句话吐成两段，别重复生成"""
        live = self.get(q)
        if live:
            return live
        k = self.key(q)
        if not k:
            return None
        best, best_r = None, 0.0
        with self._lock:
            for kk, v in self._slots.items():
                r = _lcs_len(k, kk) / max(1, min(len(k), len(kk)))
                if r > best_r:
                    best, best_r = v, r
        return best if best_r >= 0.7 else None

    def ensure(self, q, materials, cfg, picks=None):
        """有就直接返回，没有就起线程；返回可直接读的 Live（text 会持续增长）"""
        k = self.key(q)
        if not k:
            return None
        with self._lock:
            live = self._slots.get(k)
            if live and live.state in ("run", "done"):
                return live
            live = Live("rescue", q)
            self._slots[k] = live
            done = [x for x in self._slots.values() if x.state != "run"]
            if len(done) > self.KEEP:
                for old in sorted(done, key=lambda x: x.t0)[:len(done) - self.KEEP]:
                    for kk, vv in list(self._slots.items()):
                        if vv is old:
                            self._slots.pop(kk, None)

        def work():
            txt = llm_rescue(q, materials, cfg, picks=picks, on_delta=live.push)
            if txt:
                live.text = txt
                live.state = "done"
            else:
                live.state = "fail" if not live.text else "done"
                live.err = "网络或 key 问题"

        threading.Thread(target=work, daemon=True).start()
        return live


# ---------------------------------------------------------------- UI

class CueApp:
    def __init__(self, root, cfg, materials):
        self.root = root
        self.cfg = cfg
        self.materials = materials
        self.answers = load_answers()
        self.answer_mode = bool(cfg["ui"].get("answer_mode", False))
        self.collapsed = False
        self.full_h = int(cfg["ui"]["height"])
        self.w = int(cfg["ui"]["width"])
        self._drag = (0, 0)
        self._busy = False
        self._last_q = ""
        self._ui_q = []          # 工作线程 → 主线程的 UI 任务队列
        # 收音状态（供界面动画读取，跨线程只做简单赋值）
        self.asr = None
        self._state = "off"
        self._state_detail = ""
        self._rms = 0.0
        self._speaking = False
        self._phase = 0.0
        self._panic = False
        self._save_geo = ""
        self._idle_hint = "Enter 生成 · ^Enter AI兜底 · Esc 收起 · F5/F7/F8/F9"
        self._hint_seq = 0                # 底栏提示的版本号，防止旧定时器抹掉新提示
        # 后台预热：听到题就在后台把 AI 兜底跑起来
        self._pf = Prefetch()
        self._pf_q = ""          # 最近一次预热的题目
        self._live = None        # 当前正在流式显示的那块内容（Live）
        self._ai_state = ""

        root.title("面试提词器")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", float(cfg["ui"]["opacity"]))
        root.configure(bg=BG)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = cfg["ui"].get("x")
        y = cfg["ui"].get("y")
        if not isinstance(x, int) or not isinstance(y, int) or not (0 <= x < sw - 200) or not (0 <= y < sh - 200):
            x = sw - self.w - 36
            y = int(sh * 0.12)
        root.geometry("%dx%d+%d+%d" % (self.w, self.full_h, x, y))

        ui_font = ("Microsoft YaHei UI", int(cfg["ui"]["font_size"]))
        self.f_body = (ui_font[0], ui_font[1])
        self.f_small = (ui_font[0], max(9, ui_font[1] - 3))
        self.f_head = (ui_font[0], max(10, ui_font[1] - 1), "bold")

        self._build()

    # ---------- 布局
    def _build(self):
        # 顶部呼吸光条：收音状态的第一眼信号（收窄后依然可见）
        self.bar = tk.Frame(self.root, bg=BAR_OFF, height=3)
        self.bar.pack(side="top", fill="x")
        self.bar.bind("<Button-1>", lambda e: self.toggle_panic() if self._panic else None)

        # 顶栏
        self.head = tk.Frame(self.root, bg=BG2, height=34)
        self.head.pack(side="top", fill="x")
        self.head.pack_propagate(False)
        self.head.bind("<Button-1>", self._start_drag)
        self.head.bind("<B1-Motion>", self._on_drag)

        self.dot = tk.Label(self.head, text="●", fg=DIM, bg=BG2, font=self.f_small)
        self.dot.pack(side="left", padx=(10, 4))
        self.title_lb = tk.Label(self.head, text="提词", fg=FG, bg=BG2, font=self.f_head)
        self.title_lb.pack(side="left")
        self.title_lb.bind("<Button-1>", self._start_drag)
        self.title_lb.bind("<B1-Motion>", self._on_drag)

        # 收音状态文字（听得到 / 在听 / 识别中）
        self.state_lb = tk.Label(self.head, text="待机", fg=DIM, bg=BG2, font=self.f_small)
        self.state_lb.pack(side="left", padx=(8, 0))
        self.state_lb.bind("<Button-1>", lambda e: self.toggle_asr())

        close = tk.Label(self.head, text="✕", fg=DIM, bg=BG2, font=self.f_small, cursor="hand2")
        close.pack(side="right", padx=(4, 10))
        close.bind("<Button-1>", lambda e: self.quit())
        close.bind("<Enter>", lambda e: close.configure(fg="#ff6b6b"))
        close.bind("<Leave>", lambda e: close.configure(fg=DIM))

        self.mat_lb = tk.Label(self.head, text="", fg=DIM, bg=BG2, font=self.f_small,
                               cursor="hand2")
        self.mat_lb.pack(side="right", padx=6)
        self.mat_lb.bind("<Button-1>", lambda e: self.toggle_answer_mode())

        # AI 状态灯：预热中/已备好，一眼看见后台有没有在干活
        self.ai_lb = tk.Label(self.head, text="AI 待命", fg=DIM, bg=BG2, font=self.f_small)
        self.ai_lb.pack(side="right", padx=(0, 4))
        self.ai_lb.bind("<Button-1>", lambda e: self.rescue())

        # 电平条：证明它真的在听（没声音时全暗，说话时跳动）
        self.meter = tk.Canvas(self.head, width=34, height=16, bg=BG2,
                               highlightthickness=0, bd=0)
        self.meter.pack(side="right", padx=(0, 6))
        self._bars = []
        for i in range(5):
            x = 2 + i * 6
            self._bars.append(self.meter.create_rectangle(
                x, 12, x + 4, 12, fill="#2b3140", outline=""))

        # 底栏必须先于主体打包，否则会被 expand 的主体挤出窗口
        foot = tk.Frame(self.root, bg=BG2, height=24)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        self.hint = tk.Label(foot, text=self._idle_hint,
                             fg=DIM, bg=BG2, font=self.f_small)
        self.hint.pack(side="left", padx=10)

        # 主体
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(side="top", fill="both", expand=True)

        self.q_var = tk.StringVar()
        self.entry = tk.Entry(self.body, textvariable=self.q_var, bg=BG2, fg=FG,
                              insertbackground=ACCENT, relief="flat",
                              font=self.f_body, highlightthickness=1,
                              highlightbackground="#262b36", highlightcolor=ACCENT)
        self.entry.pack(fill="x", padx=10, pady=(10, 8), ipady=7)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Escape>", lambda e: self.toggle_collapse())
        self._placeholder()

        wrap = tk.Frame(self.body, bg=BG)
        wrap.pack(fill="both", expand=True, padx=10)
        self.txt = tk.Text(wrap, bg=BG, fg=FG, relief="flat", wrap="word",
                           font=self.f_body, padx=2, pady=2, spacing1=2, spacing3=6,
                           insertbackground=BG, highlightthickness=0, cursor="arrow")
        sb = tk.Scrollbar(wrap, command=self.txt.yview, width=8,
                          troughcolor=BG, bg="#2b3140", relief="flat", bd=0)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("dim", foreground=DIM)
        self.txt.tag_configure("accent", foreground=ACCENT)
        self.txt.tag_configure("warn", foreground=WARN)
        self.txt.tag_configure("head", foreground="#ffffff",
                               font=(self.f_body[0], self.f_body[1] + 1, "bold"))
        self.txt.configure(state="disabled")

        self.root.bind("<Escape>", lambda e: self.toggle_collapse())
        self.root.bind("<Control-Return>", lambda e: self.rescue())
        self.root.bind("<F5>", lambda e: self.reload_materials())
        self.root.bind("<F7>", lambda e: self.toggle_answer_mode())
        self.root.bind("<F8>", lambda e: self.toggle_asr())
        self.root.bind("<F9>", lambda e: self.toggle_panic())
        self.root.bind("<Control-q>", lambda e: self.quit())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self._update_head()
        self._welcome()
        self._init_asr()
        # 提前把 AI 连接建好：新进程第一枪首字要十几秒，这一枪 0.2 秒就回来
        if self._llm_ready():
            threading.Thread(target=llm_warmup, args=(self.cfg,), daemon=True).start()
        self.root.after(150, self._apply_capture_guard)
        self.root.after(60, self._tick)

    def _placeholder(self):
        self._ph_on = True
        self.entry.insert(0, "听清问题后，输入或粘贴一句话，回车")
        self.entry.configure(fg="#7a8296")
        self.entry.bind("<FocusIn>", self._ph_off)

    def _ph_off(self, _=None):
        if self._ph_on:
            self.entry.delete(0, "end")
            self.entry.configure(fg=FG)
            self._ph_on = False

    def _welcome(self):
        msg = [
            ("提词器就绪", "head"),
            ("", None),
            ("素材 %d 条 · 已加载 resume.md" % len(self.materials), "dim"),
            ("", None),
            ("用法：听清问题 → 敲几个关键词或整句 → 回车", None),
            ("输出是给你看的提纲，不是给你念的稿子。", "dim"),
            ("", None),
            ("Esc 收成一条窄条，面试中随时收。", "dim"),
        ]
        if not self.materials:
            msg += [("", None), ("⚠ data/resume.md 还是空的，先填素材", "warn")]
        self._render(msg)

    def _render(self, blocks):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        for text, tag in blocks:
            self.txt.insert("end", text + "\n", tag or ())
        self.txt.configure(state="disabled")

    # ---------- 收音（v0.2）
    def _init_asr(self):
        cfg = self.cfg.get("asr", {}) or {}
        if not cfg.get("enabled", True):
            self._set_state("off")
            return
        model = BASE / "models" / ("faster-whisper-%s" % cfg.get("model", "small"))
        if not (model / "model.bin").exists():
            self._set_state("error", "模型缺失：%s" % model.name)
            return
        try:
            import asr as asr_mod
        except Exception:
            self._set_state("error", "缺依赖：pip install faster-whisper")
            return
        self._asr_mod = asr_mod
        self._auto_cue = bool(cfg.get("auto_cue", True))
        try:
            self.asr = asr_mod.AsrService(
                model,
                on_text=self._on_asr_text,
                on_status=self._on_asr_status,
                on_level=self._on_asr_level,
                mode=cfg.get("mode", "loopback"),
                silence_ms=int(cfg.get("silence_ms", 750)),
                hotwords=cfg.get("hotwords", ""),
                device_index=int(cfg.get("device_index", -1)),
            )
        except Exception as e:
            self._set_state("error", "初始化失败：%s" % e)
            return
        self._set_state("loading")
        threading.Thread(target=self._start_asr_safe, daemon=True).start()

    def _start_asr_safe(self):
        try:
            if self._llm_ready():
                threading.Thread(target=llm_warmup, args=(self.cfg,), daemon=True).start()
            self.asr.start()
        except Exception as e:
            msg = str(e)
            self._post(lambda: self._set_state("error", msg))

    def toggle_asr(self):
        """F8：一键停/开收音。开会、自己思考时关掉，省 CPU 也避免乱弹"""
        if self.asr is None:
            self._say("收音不可用，检查 models/ 与依赖", WARN, 3000)
            return
        if self._state in ("off", "error"):
            self._set_state("loading")
            threading.Thread(target=self._start_asr_safe, daemon=True).start()
        else:
            self.asr.stop()
            self._set_state("off")

    def _set_state(self, state, detail=""):
        self._state = state
        self._state_detail = detail
        txt, color = {
            "off": ("收音关 F8 开", DIM),
            "loading": ("加载中…", WARN),
            "listening": ("收音中", ACCENT),
            "transcribing": ("识别中", WARN),
            "error": ("出错", RED),
        }.get(state, ("待机", DIM))
        self.state_lb.configure(text=txt, fg=color)
        if state == "error" and detail:
            self._say("⚠ " + detail[:42], RED, 5000)

    # 以下三个由采集/识别线程调用：一律排队回主线程，不在工作线程里碰 tkinter
    def _on_asr_status(self, state, detail=""):
        self._post(lambda: self._set_state(state, detail))

    def _on_asr_level(self, rms, speaking):
        self._rms = rms
        self._speaking = speaking

    def _on_asr_text(self, text, meta):
        if not text:
            return
        self._post(lambda: self._apply_asr_text(text, meta))

    def _apply_asr_text(self, text, meta):
        if not text:
            # 识别出来了但没过问句闸门 / 被当成噪声过滤掉：说清楚卡在哪一环，
            # 否则用户只会觉得"识别不出来"，根本没法判断是音量、设备还是判句
            raw = (meta.get("raw") or "").strip()
            peak = float(meta.get("peak") or 0)
            db = int(20 * math.log10(peak)) if peak > 1e-6 else -99
            if raw:
                msg = "听到「%s」但不像提问 ｜ 音量 %ddB（按 Enter 手动出）" % (raw[:16], db)
                color = WARN
            elif meta.get("dropped"):
                msg = "这段没识别人声（噪声已过滤）｜ 音量 %ddB" % db
                color = DIM
            else:
                msg = "没听清 ｜ 音量 %ddB（检查系统音量/输出设备）" % db
                color = WARN
            if msg != getattr(self, "_last_skip_msg", ""):
                self._last_skip_msg = msg
                self._say(msg, color)
            return
        self.q_var.set(text)
        self._last_skip_msg = ""
        self._ph_on = False
        self.entry.configure(fg=FG)
        if self._auto_cue:
            self.generate()      # 里面顺手把兜底答案放后台预热
        else:
            self._prefetch_only(text)
        self._say("🎙 %s ｜ 识别 %dms" % (text[:22], int(meta.get("asr_ms", 0))), ACCENT, 4500)
        if self.collapsed:
            # 收窄状态不自动展开（可能正被共享屏幕），只提示有新内容
            self.state_lb.configure(text="有提示 Esc", fg=GREEN)

    def _prefetch_only(self, q):
        """没开自动弹提纲时，也把 AI 兜底先跑起来——只花 token，不动界面"""
        if not self._llm_ready():
            return
        try:
            _, _, picks, level = build_local_cue(q, self.materials)
        except Exception:
            return
        self._maybe_prefetch(q, picks, level)

    def _sync_ai_pill(self):
        """预热的进度靠 tick 反映到顶栏，不用工作线程碰控件"""
        if not self._pf_q:
            return
        live = self._pf.get(self._pf_q)
        if not live:
            return
        if live.state == "run":
            self._ai_pill("warm")
        elif live.state == "done":
            self._ai_pill("ready")
        else:
            self._ai_pill("fail")

    def _say(self, text, color, ms=4000):
        """写底栏并按时复位。

        必须带序号：防截屏提示在启动 6 秒后会定时复位，原来会把中途新写的提示一起
        抹掉（"AI 这次慢"就正好被抹过一次）。只有最新那条的定时器才有权复位。
        """
        self._hint_seq += 1
        seq = self._hint_seq
        self.hint.configure(text=text, fg=color)
        self.root.after(ms, lambda: self._reset_hint(seq))

    def _reset_hint(self, seq=None):
        if seq is not None and seq != self._hint_seq:
            return
        self.hint.configure(text=self._idle_hint, fg=DIM)

    # ---------- 防截屏 / 应急隐身
    def _apply_capture_guard(self):
        """启动时把自己从屏幕捕获里摘出去：本机看得见，共享画面里不存在"""
        cfg_ui = self.cfg.get("ui", {}) or {}
        if not cfg_ui.get("exclude_from_capture", True):
            return
        self.root.update_idletasks()
        self._guard_ok = set_capture_excluded(self.root, True)
        if self._guard_ok:
            self._say("防截屏已开：共享屏幕时对方看不到本窗口", ACCENT, 6000)
        else:
            self._say("防截屏不可用（需 Win10 2004+），共享时请按 F9 缩条", WARN, 6000)


    def toggle_panic(self):
        """F9：缩成 88×10 小条应急（摄像头拍屏、有人靠近时）；点它或再按 F9 还原"""
        if not self._panic:
            self._panic = True
            self._save_geo = "%dx%d+%d+%d" % (self.w, self.full_h,
                                             self.root.winfo_x(), self.root.winfo_y())
            self.root.geometry("88x10+%d+%d" % (self.root.winfo_screenwidth() - 96, 2))
            self.bar.configure(bg=BAR_OFF)
            self.dot.configure(fg=DIM)
        else:
            self._panic = False
            self.root.geometry(self._save_geo or ("%dx%d" % (self.w, self.full_h)))
            self._set_state(self._state)

    def _post(self, fn):
        """从工作线程把 UI 更新排队给主线程——tkinter 禁止跨线程碰控件"""
        self._ui_q.append(fn)

    def _drain_ui(self):
        while self._ui_q:
            fn = self._ui_q.pop(0)
            try:
                fn()
            except Exception:
                pass

    def _tick(self):
        """60ms 一次：呼吸光条 + 电平条，用颜色证明它真的在听"""
        self._drain_ui()
        self._draw_live()          # 流式内容边到边画
        self._sync_ai_pill()
        if self._panic:
            self.root.after(400, self._tick)
            return
        self._phase = (self._phase + 0.055) % 1.0
        k = (math.sin(self._phase * 2 * math.pi) + 1) / 2
        st = self._state
        speaking = (st == "listening" and self._speaking)

        if st == "listening":
            color = _blend(BAR_DIM, GREEN if speaking else ACCENT, 0.3 + 0.7 * k)
        elif st == "transcribing":
            color = _blend(BAR_DIM, WARN, 0.45 + 0.55 * k)
        elif st == "loading":
            color = _blend(BAR_DIM, WARN, 0.12 + 0.3 * k)
        elif st == "error":
            color = "#6e2a2a"
        else:
            color = BAR_OFF
        self.bar.configure(bg=color)
        self.dot.configure(fg=color if st != "off" else DIM)

        # 电平：对数刻度，说话时跳，静音时全暗
        r = self._rms
        lv = 0.0
        if r > 0.0015:
            lv = max(0.0, min(1.0, (math.log10(r / 0.0015) + 1.0) / 2.2))
        lit = int(round(lv * 5))
        on_color = GREEN if speaking else ACCENT
        for i, item in enumerate(self._bars):
            h = 4 + i * 2
            self.meter.coords(item, 2 + i * 6, 12 - h, 6 + i * 6, 12)
            self.meter.itemconfigure(item, fill=on_color if i < lit else "#2b3140")

        if st == "listening" and not self.collapsed:
            self.state_lb.configure(text="在听" if speaking else "收音中",
                                    fg=GREEN if speaking else ACCENT)

        self.root.after(60, self._tick)

    # ---------- 交互
    def _start_drag(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _on_drag(self, e):
        self.root.geometry("+%d+%d" % (e.x_root - self._drag[0], e.y_root - self._drag[1]))

    def quit(self):
        """退出前记住窗口位置，下次开在原处"""
        try:
            if self.asr:
                self.asr.stop()
        except Exception:
            pass
        try:
            cfg = load_config()
            cfg["ui"]["x"] = self.root.winfo_x()
            cfg["ui"]["y"] = self.root.winfo_y()
            cfg["ui"]["answer_mode"] = self.answer_mode
            CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self.root.destroy()

    def reload_materials(self):
        """改完 resume.md 不用重启，F5 重载"""
        self.materials = load_materials()
        self.answers = load_answers()
        self._update_head()
        self._say("已重载：素材 %d 条 · 答案 %d 条"
                  % (len(self.materials), len(self.answers)), ACCENT, 2200)

    def _update_head(self):
        """顶栏右侧的模式标识：点一下或 F7 切换"""
        if self.answer_mode:
            self.mat_lb.configure(text="答案模式 %d" % len(self.answers), fg=ACCENT)
        else:
            self.mat_lb.configure(text="要点模式 %d" % len(self.materials), fg=DIM)

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            self.root.geometry("%dx34" % self.w)
            self.dot.configure(fg="#5a6272")
            self.title_lb.configure(text="提词（Esc 展开）")
        else:
            self.body.pack(side="top", fill="both", expand=True)
            self.root.geometry("%dx%d" % (self.w, self.full_h))
            self.dot.configure(fg=ACCENT)
            self.title_lb.configure(text="提词")
            self._set_state(self._state)
            self.entry.focus_set()

    def _render_cue(self, text):
        blocks = []
        for i, ln in enumerate(text.split("\n")):
            if i == 0:
                blocks.append((ln, "head"))
            elif ln.startswith("💡"):
                blocks.append((ln, "accent"))
            elif ln.startswith("·") or ln.startswith("（") or ln.startswith("——"):
                blocks.append((ln, "dim"))
            else:
                blocks.append((ln, None))
        self._render(blocks)

    def generate(self):
        q = self.q_var.get().strip()
        if not q or self._ph_on:
            return
        self._last_q = q
        local, key, picks, level = build_local_cue(q, self.materials)
        self._live = None                    # 新题进来，旧的流式内容作废

        # 技术题不拿素材凑数：「接口请求方式有几种」会被检索撞到「接口测试流程」，
        # 那是你的项目经历，答不了它。这类题直接让 AI 给口语答案。
        if key != "reverse" and is_technical(q) and self._llm_ready():
            self._answer_tech(q, picks, local)
            return

        if self.answer_mode and key != "reverse":
            txt, hit = build_answer(q, self.answers, picks)
            if txt:
                self._render_cue(txt)
                self._ai_pill("idle")
                self._log(q, key, txt)
                return
            local += "\n\n（这道题还没生成答案草稿：python gen_answers.py）"
            level = "miss"                   # 没草稿就等于没准备，值得让 AI 在后台备一份
        self._render_cue(local)
        if not self.answer_mode and (self.cfg.get("llm") or {}).get("enabled"):
            self._start_cue(q, key, picks)   # 实时提纲：边生成边显示
        else:
            self._maybe_prefetch(q, picks, level)
        self._log(q, key, local)

    def _answer_tech(self, q, picks, local):
        """技术题走这条路：后台现成的直接画，没有就边生成边显示。

        AI 不成（没网/断 key）时退回本地素材，别留一块空白让人干等。
        """
        live = self._pf.find(q)
        if live is None:
            live = self._pf.ensure(q, self.materials, self.cfg, picks)
            self._pf_q = q
        self._live = live
        if live.state == "fail":
            self._render_cue(local)
            self._say("AI 没答上来（网络/key）｜先按上面的素材说", WARN)
            return
        self._ai_pill("ready" if live.state == "done" else "warm")
        self._draw_live(force=True)          # 已备好=瞬间出；在跑=首字到了就补

    def _llm_ready(self):
        llm = self.cfg.get("llm", {}) or {}
        return bool(llm.get("api_key") and llm.get("base_url"))

    def _ai_pill(self, state):
        """顶栏 AI 状态灯——后台在不在干活，一眼就能看见"""
        if state == self._ai_state:
            return
        self._ai_state = state
        txt, color = {
            "off": ("AI 关", DIM),
            "idle": ("AI 待命", DIM),
            "warm": ("AI 预热中", WARN),
            "ready": ("AI 已备好 ^↵", GREEN),
            "fail": ("AI 未成", RED),
        }.get(state, ("AI", DIM))
        try:
            self.ai_lb.configure(text=txt, fg=color)
        except Exception:
            pass

    def _maybe_prefetch(self, q, picks, level):
        """听到题的瞬间就在后台把兜底答案生成好：等你要用时它已经在手里

        - 技术题一律预热：本地素材是"我的项目"，对"TCP 和 UDP 区别"这种题没用，
          真正要用的是 AI 现答；命中素材也照样备一份，反正一分钱。
        - 经历题只在题库没覆盖好（miss/near）时预热，命中题库的本地就有，不重复烧 token。
        - 同一句话被 ASR 吐成两遍（截断/补全）不会重复跑。
        """
        if not self._llm_ready():
            self._ai_pill("off")
            return
        if not (self.cfg.get("llm") or {}).get("prefetch", True):
            return
        if level == "hit" and not is_technical(q):
            self._pf_q = ""                  # 这题不用 AI，别让状态灯被上一题带跑
            self._ai_pill("idle")
            return
        same = self._pf.find(q)
        if same:
            self._pf_q = same.q              # 已有同一句的结果（跑着或已好），复用
            self._ai_pill("warm" if same.state == "run" else "ready")
            return
        self._pf_q = q
        live = self._pf.ensure(q, self.materials, self.cfg, picks)
        self._ai_pill("warm" if live and live.state == "run" else "ready")

    def toggle_answer_mode(self):
        self.answer_mode = not self.answer_mode
        self.cfg["ui"]["answer_mode"] = self.answer_mode
        self._update_head()
        self._set_state(self._state)
        if self._last_q:
            self.q_var.set(self._last_q)
            self._ph_on = False
            self.generate()

    def _on_return(self, event=None):
        """回车=出提纲；Ctrl+回车=让 AI 现场兜底（不自动跑，只在你要的时候）"""
        if event is not None and (event.state & 0x0004):     # 0x0004 = Control
            self.rescue()
            return "break"
        self.generate()

    def rescue(self):
        """题库里没有的题：拿下「接-移-收」三段。后台预热过的直接出，不再等"""
        q = self.q_var.get().strip()
        if not q or self._ph_on:
            return
        if not self._llm_ready():
            self._say("AI 兜底要 key：填 data/config.json 的 llm.api_key", WARN)
            return
        self._last_q = q
        live = self._pf.find(q)
        if live and live.state == "done" and live.text:
            self._live = live
            self._draw_live(force=True)
            self._say("AI 兜底好了｜后台已备 %.1fs，直接说" % live.elapsed(), GREEN)
            return
        if live is None or live.state == "fail":
            _, _, picks, _ = build_local_cue(q, self.materials)
            live = self._pf.ensure(q, self.materials, self.cfg, picks)
            self._pf_q = q
        # 已经在后台跑：接管显示，写到哪看到哪，不用干等整段
        self._live = live
        self._ai_pill("warm")
        self._say("AI 兜底生成中…先看已有内容", ACCENT, 8000)
        self._draw_live(force=True)

    def _start_cue(self, q, key, picks):
        """实时提纲：首字一到就重绘，剩下的字边到边补"""
        live = Live("cue", q, FRAMEWORKS[key][0])
        self._live = live
        self._ai_pill("warm")

        def work():
            txt = llm_cue(q, self.materials, self.cfg, picks=picks,
                          on_delta=live.push)
            live.text = txt or live.text
            live.state = "done" if (txt or live.text) else "fail"

        threading.Thread(target=work, daemon=True).start()

    def _draw_live(self, force=False):
        """把 Live 里已生成的部分画出来；没变化就不重绘，60ms 一次足够顺"""
        live = self._live
        if not live:
            return
        # 上游偶尔抽风十几秒不出字，这时"首字 0.3 秒内到"是在骗人——先提醒别干等。
        # 放在快照比较之前：内容没更新也要能弹出这句。
        if live.state == "run" and not live.text and live.elapsed() > 6 and not live.slow:
            live.slow = True
            self._say("AI 这次慢（%.0fs 还没出字）｜先照素材讲，别等" % live.elapsed(),
                      WARN, 5000)
        snap = (live.text, live.state, live.slow)
        if not force and snap == live.drawn:
            return
        live.drawn = snap
        if live.kind == "rescue":
            self._render_rescue(live)
        else:
            self._render_cue_ai(live)
        if live.state == "done":
            self._ai_pill("ready")
            if live.kind == "rescue":
                if not live.logged:
                    live.logged = True
                    self._log(live.q, "rescue", live.text)
                self._say(("AI 作答好了（%.1fs）｜照意思说" if is_technical(live.q)
                           else "AI 兜底好了（%.1fs）｜用自己的话说") % live.elapsed(),
                          ACCENT)
        elif live.state == "fail":
            self._ai_pill("fail")
            self._say("AI 兜底失败（网络/key）｜按上面的结构自己接", WARN)

    def _render_rescue(self, live):
        head = ("▍ AI 作答 ｜ 技术题，直接讲出来" if is_technical(live.q)
                else "▍ AI 兜底 ｜ 照意思说，别背原句")
        if live.state == "run":
            head += "   ✍"
        blocks = [(head, "head"), ("", None)]
        lines = [l.strip() for l in live.text.splitlines() if l.strip()]
        for s in lines:
            blocks.append((s, "accent" if s[:1] in "答深例接移收" else None))
        if not lines:
            blocks.append(("（AI 这次没回，先照上面的素材讲）" if live.slow
                           else "（首字 0.3 秒内到，先别停…）", "dim"))
        elif live.state == "run":
            blocks.append(("▌", "dim"))
        self._render(blocks)

    def _render_cue_ai(self, live):
        blocks = [("▍ %s ｜ AI 提纲" % live.head, "head"), ("", None)]
        for ln in live.text.splitlines():
            s = ln.strip()
            if not s:
                blocks.append(("", None))
            elif s.startswith("💡"):
                blocks.append((s, "accent"))
            else:
                blocks.append((s, None))
        if live.state == "fail":
            blocks.append(("（生成失败，看上面的本地要点）", "dim"))
        self._render(blocks)

    def _log(self, q, key, cue):
        try:
            SESSIONS.mkdir(parents=True, exist_ok=True)
            f = SESSIONS / (datetime.now().strftime("%Y-%m-%d") + ".md")
            first = [l for l in cue.split("\n") if l.strip()][:2]
            with f.open("a", encoding="utf-8") as fh:
                fh.write("\n### %s ｜ %s\n- 问题：%s\n- 类型：%s\n- 首条要点：%s\n"
                         % (datetime.now().strftime("%H:%M"), FRAMEWORKS[key][0], q, key,
                            first[1] if len(first) > 1 else ""))
        except Exception:
            pass


def enable_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_capture_excluded(root, enable=True):
    """让窗口对屏幕捕获不可见（Win10 2004+）。本机照常可见，共享/录屏里看不到。

    WDA_EXCLUDEFROMCAPTURE=0x11 会从捕获画面里整个抹掉窗口；
    老系统退到 WDA_MONITOR=0x01，捕获里是一块黑，至少不露内容。
    """
    try:
        u = ctypes.windll.user32
        hwnd = u.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
        if not hwnd:
            hwnd = root.winfo_id()
        if not enable:
            return bool(u.SetWindowDisplayAffinity(hwnd, 0x00))
        if u.SetWindowDisplayAffinity(hwnd, 0x11):
            return True
        return bool(u.SetWindowDisplayAffinity(hwnd, 0x01))
    except Exception:
        return False


def main():
    enable_dpi()
    cfg = load_config()
    materials = load_materials()

    if "--ask" in sys.argv:
        i = sys.argv.index("--ask")
        words = [a for a in sys.argv[i + 1:] if not a.startswith("--")]
        q = " ".join(words) or "介绍一下你自己"
        out, key, picks, level = build_local_cue(q, materials)
        if "--answer" in sys.argv:
            txt, hit = build_answer(q, load_answers(), picks)
            print(txt or "（这道题还没有答案草稿：python gen_answers.py）")
        elif "--ai" in sys.argv:
            t0, first = time.time(), []
            def on_d(ch):
                if not first:
                    first.append(time.time() - t0)
                sys.stdout.write(ch)
                sys.stdout.flush()
            txt = llm_rescue(q, materials, cfg, picks=picks, on_delta=on_d)
            if not txt:
                print("（失败：查 key / base_url / 网络）")
            print("\n[首字 %s · 总 %.2fs]"
                  % ("%.2fs" % first[0] if first else "—", time.time() - t0))
        else:
            print(out)
        return

    root = tk.Tk()
    app = CueApp(root, cfg, materials)
    root.update_idletasks()
    root.attributes("-alpha", 0.0)
    root.after(30, lambda: root.attributes("-alpha", float(cfg["ui"]["opacity"])))
    if "--selftest" in sys.argv:
        root.update()
        app.q_var.set("介绍一下你做过最有挑战的项目")
        app._ph_on = False
        app.generate()
        root.update()
        time.sleep(0.2)
        root.destroy()
        print("SELFTEST OK")
        return
    root.mainloop()


if __name__ == "__main__":
    main()
