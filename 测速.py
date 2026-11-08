# -*- coding: utf-8 -*-
"""模型测速：测「首字延迟」和「整段耗时」，判断哪家模型适合面试现场用。

用法：
    python 测速.py                              # 用 data/config.json 里的配置测一遍
    python 测速.py --preset qwen-flash --key sk-xxx
    python 测速.py --preset glm-flash,deepseek --key sk-xxx   # 同 key 不行，逐家测要分别给 key
    python 测速.py --base-url https://xxx/v1 --model 模型名 --key sk-xxx

面试场景里真正要紧的是「首字延迟」：第一个字多久出现，决定你有没有话接。
整段耗时再长也没关系——字是边生成边显示的。
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "data" / "config.json"
CONFIG_LOCAL = BASE / "data" / "config.local.json"   # 放 key 的本地文件，不进版本库

# 常见 OpenAI 兼容服务（base_url, model, 备注）
PRESETS = {
    "deepseek":  ("https://api.deepseek.com/v1", "deepseek-chat", "便宜、稳、首字 0.3-0.7s"),
    "qwen-flash": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-flash", "阿里百炼，首字常 <0.4s"),
    "qwen-turbo": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo", "更便宜，略慢于 flash"),
    "glm-flash": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "智谱，免费额度，很快"),
    "moonshot":  ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "国内直连稳"),
    "silicon":   ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", "小模型，首字极快"),
    "openai":    ("https://api.openai.com/v1", "gpt-4o-mini", "需梯子，首字 0.4s 左右"),
}

SYS = "输出三段，每段一行，行首依次「接：」「移：」「收：」，每行不超过 30 字，口语。"
ASK = "【面试官问】你怎么理解全链路压测？"


def probe(base_url, model, key, rounds=3):
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": ASK}],
        "temperature": 0.4, "max_tokens": 190, "stream": True,
    }).encode("utf-8")
    ttfts, totals, text = [], [], ""
    for _ in range(rounds):
        # 每轮新建 Request：复用同一个对象再发，部分网关会退回 GET 报 405
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key})
        t0 = time.time()
        first = None
        buf = []
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                for raw in r:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    p = line[5:].strip()
                    if p == "[DONE]":
                        break
                    try:
                        d = json.loads(p)
                    except Exception:
                        continue
                    ch = (d.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                    if ch:
                        if first is None:
                            first = time.time() - t0
                        buf.append(ch)
        except Exception as e:
            return None, None, str(e)
        ttfts.append(first or (time.time() - t0))
        totals.append(time.time() - t0)
        text = "".join(buf)
    return min(ttfts), sum(totals) / len(totals), text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="", help="逗号分隔：" + " / ".join(PRESETS))
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--ask", action="store_true", help="打印模型实际输出")
    a = ap.parse_args()

    cfg = {}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8")).get("llm", {})
        if CONFIG_LOCAL.exists():          # key 被移出仓库，真实值放这里
            try:
                cfg.update(json.loads(CONFIG_LOCAL.read_text(encoding="utf-8")).get("llm", {}))
            except Exception:
                pass
        cfg["api_key"] = os.environ.get("INTERVIEW_LLM_KEY") or cfg.get("api_key", "")

    jobs = []
    if a.base_url and a.model:
        jobs.append((a.base_url, a.model, a.model, "自定义"))
    for name in [x.strip() for x in a.preset.split(",") if x.strip()]:
        if name not in PRESETS:
            print("不认识的 preset：%s（可选：%s）" % (name, " / ".join(PRESETS)))
            continue
        u, m, note = PRESETS[name]
        jobs.append((u, m, name, note))
    if not jobs:
        if not (cfg.get("base_url") and cfg.get("model")):
            print("没有可测的配置：先在 data/config.json 填 llm，或用 --preset")
            return
        jobs.append((cfg["base_url"], cfg["model"], cfg["model"], "config.json 里的配置"))

    key = a.key or cfg.get("api_key", "")
    if not key:
        print("缺 key：--key sk-xxx，或先填 data/config.json 的 llm.api_key")
        return

    print("%-14s %10s %10s  %s" % ("模型", "首字", "整段", "备注"))
    print("-" * 62)
    for base_url, model, name, note in jobs:
        ttft, total, extra = probe(base_url, model, key)
        if ttft is None:
            print("%-14s %10s %10s  %s" % (name, "失败", "-", extra[:40]))
            continue
        print("%-14s %8.2fs %8.2fs  %s" % (name, ttft, total, note))
        if a.ask and extra:
            print("   →", extra.replace("\n", " / ")[:100])
    print("\n提示：首字 <0.7s 基本无感；换模型只要改 data/config.json 的 base_url + model")


if __name__ == "__main__":
    sys.exit(main())
