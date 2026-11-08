# -*- coding: utf-8 -*-
"""批量生成答案草稿。

把 data/resume.md 里每条素材改写成"你自己会说出来的口头回答"，写进 data/answers.md。
特性：
  - 断点续跑：已经生成过的条目自动跳过，中断后重跑即可
  - 边生成边落盘：每完成一条就重写 answers.md，中途崩了不丢
  - answers.md 是唯一数据源：你手动改过的内容不会被覆盖（除非加 --redo）

用法：
  python gen_answers.py              # 生成缺的部分
  python gen_answers.py --limit 5    # 只生成 5 条（试水）
  python gen_answers.py --redo       # 全部重生成（会覆盖手改内容）
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
RESUME = DATA / "resume.md"
ANSWERS = DATA / "answers.md"
CONFIG = DATA / "config.json"

SYS_PROMPT = """你是面试教练，把候选人的素材改写成他本人会说出来的口头回答。

【绝对禁止】不得新增素材里没有的任何事实：不许编工具名、模块名、时间、数字、他人反应、上线结果、你不知道的具体做法。素材没写到的，就不说。

【允许】把素材里已有的要点重新组织成连贯口语，可以加连接词、态度和判断（"我觉得""回头想想"），可以调整顺序。

【写法】
1) 第一人称，说人话，短句为主，不要书面语、不要排比、不要"首先其次"；
2) 结构：结论 → 具体做法 → 结果 → 一句反思；
3) 素材里的数字必须用上，且不许改动；
4) 篇幅 150-240 汉字。素材不够就写短一点，宁可短，不许编；
5) 只输出答案正文，不要标题、不要解释。"""


# ------------------------------------------------------------------ 解析

def strip_title(body):
    """'- 【标题】正文' / 已去掉 '- ' 的字符串 → (标题, 正文)"""
    m = re.match(r"^[【\[](.*?)[】\]](.*)$", body)
    if m:
        return m.group(1).strip(), m.group(2).strip(" ｜|")
    return "", body.strip()


def tidy(text):
    """把素材里的 '｜' 分隔符改成逗号，短条目直接当时答案时才不像清单"""
    parts = [p.strip(" 。") for p in re.split(r"[｜|]", text) if p.strip(" 。")]
    out = "，".join(parts)
    return out


def parse_resume():
    """→ [{'section':..., 'title':..., 'body':...}]，保持原顺序"""
    if not RESUME.exists():
        sys.exit("找不到 %s" % RESUME)
    items, section = [], "通用"
    for ln in RESUME.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("## "):
            section = s[3:].strip()
            continue
        if not (s.startswith("- ") and len(s) > 3):
            continue
        body = s[2:].strip()
        if body.startswith("(") or body.startswith("（"):
            continue
        title, text = strip_title(body)
        if not title:                      # 没写标题的用正文前 14 字兜底
            title, text = text[:14], text
        items.append({"section": section, "title": title, "body": text})
    return items


def parse_answers():
    """读回已有的 answers.md → {(section, title): answer}"""
    out = {}
    if not ANSWERS.exists():
        return out
    section, title, buf = None, None, []

    def flush():
        if title and buf:
            out[(section, title)] = "\n".join(buf).strip()

    for ln in ANSWERS.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            flush()
            section, title, buf = ln[3:].strip(), None, []
        elif ln.startswith("### "):
            flush()
            title, buf = ln[4:].strip(), []
        elif title is not None:
            buf.append(ln)
    flush()
    return out


def render(items, answers):
    """按素材原顺序输出 markdown，未生成的条目留占位"""
    lines = [
        "# 预生成答案库 · 面试回答草稿",
        "",
        "> 由 `gen_answers.py` 基于 `data/resume.md` 生成。**这是草稿，不是让你背的稿子。**",
        "> 用法：改成人话 → 存盘 → 提词器按 `F5` → `F7` 切到答案模式。",
        "> 改过的句子就是你的了；没改的句子 AI 味重，真实面试别照念。",
        "",
    ]
    last_sec = None
    for it in items:
        key = (it["section"], it["title"])
        ans = answers.get(key)
        if not ans:
            continue
        if it["section"] != last_sec:
            lines += ["## " + it["section"], ""]
            last_sec = it["section"]
        lines += ["### " + it["title"], "", ans, ""]
    return "\n".join(lines)


def save(items, answers):
    tmp = ANSWERS.with_suffix(".tmp")
    tmp.write_text(render(items, answers), encoding="utf-8")
    tmp.replace(ANSWERS)


# ------------------------------------------------------------------ 调模型

def load_cfg():
    llm = json.loads(CONFIG.read_text(encoding="utf-8")).get("llm", {})
    local = DATA / "config.local.json"     # key 不进版本库，真实值放这里
    if local.exists():
        try:
            llm.update(json.loads(local.read_text(encoding="utf-8")).get("llm", {}))
        except Exception:
            pass
    llm["api_key"] = os.environ.get("INTERVIEW_LLM_KEY") or llm.get("api_key", "")
    if not (llm.get("base_url") and llm.get("api_key")):
        sys.exit("config.json 里 llm.base_url / api_key 没填")
    return llm


def call_api(llm, section, title, body, retries=3):
    user = "【所属主题】%s\n\n【本题素材】\n%s\n\n【面试官会这样问】\n%s" % (
        section, body, title)
    payload = json.dumps({
        "model": llm.get("model") or "deepseek-chat",
        "messages": [{"role": "system", "content": SYS_PROMPT},
                     {"role": "user", "content": user}],
        "temperature": 0.5,
        "max_tokens": 700,
    }).encode("utf-8")
    url = llm["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    last = None
    for i in range(retries):
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + llm["api_key"],
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            last = "HTTP %s %s" % (e.code, body_txt)
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            raise RuntimeError(last)
        except Exception as e:
            last = "%s %s" % (type(e).__name__, e)
            time.sleep(2 * (i + 1))
    raise RuntimeError(last or "unknown")


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只生成 N 条")
    ap.add_argument("--redo", action="store_true", help="已有的也重新生成")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    llm = load_cfg()
    items = parse_resume()
    answers = {} if args.redo else parse_answers()
    had = len(answers)

    todo = [it for it in items if (it["section"], it["title"]) not in answers]
    # 素材太短的（"一周内"这种）不必调模型，直接当答案
    short = [it for it in todo if len(it["body"]) < 20]
    todo = [it for it in todo if len(it["body"]) >= 20]

    for it in short:
        answers[(it["section"], it["title"])] = tidy(it["body"])

    if args.limit:
        todo = todo[:args.limit]

    total = len(items)
    print("素材 %d 条｜原有答案 %d｜本次生成 %d｜短条目直接复用 %d｜仍缺 %d"
          % (total, had, len(todo), len(short),
             total - len(answers) - len(todo)))
    if not todo:
        save(items, answers)
        print("没有需要生成的，answers.md 已是最新")
        return

    lock = threading.Lock()
    done, failed = [0], []
    t0 = time.time()

    def work(it):
        if len(it["body"]) < 20:
            return it, it["body"]
        return it, call_api(llm, it["section"], it["title"], it["body"])

    save(items, answers)          # 先落一次盘，保证短答案不丢
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, it): it for it in todo}
        for fut in futs:
            it = futs[fut]
            try:
                _, ans = fut.result()
                with lock:
                    answers[(it["section"], it["title"])] = ans
                    done[0] += 1
                    save(items, answers)
                    el = time.time() - t0
                    eta = el / done[0] * (len(todo) - done[0])
                    print("  [%3d/%3d] %-6s %s  (%.0fs 剩余)"
                          % (done[0], len(todo), it["section"][:6], it["title"], eta))
            except Exception as e:
                failed.append((it["title"], str(e)))
                print("  [失败] %s → %s" % (it["title"], str(e)[:120]))

    save(items, answers)
    print("\n完成 %d 条，失败 %d 条，用时 %.0f 秒" % (done[0], len(failed), time.time() - t0))
    print("输出：%s（%d 条答案）" % (ANSWERS, len(answers)))
    if failed:
        print("失败清单（重跑会自动补）：")
        for t, e in failed[:10]:
            print("  - %s: %s" % (t, e))


if __name__ == "__main__":
    main()
