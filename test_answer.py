# -*- coding: utf-8 -*-
"""兜底/作答路径回归（不联网：用假的 llm_rescue 顶替）

验三件事：
1) 技术题回车即出 AI 答案，不用按快捷键，也不会被不相干的素材顶掉；
2) 经历题仍走素材/答案草稿，不误调模型；
3) 素材完全不相关（0 分）时，答案库不许念错稿子。
"""
import time
import tkinter as tk

import app

FAKE = ["答：常用的有 GET、POST、PUT、DELETE。\n",
        "深：GET 查、POST 增、PUT 改、DELETE 删。\n",
        "例：我测 ERP 接口时按文档核对每个接口的请求方式。"]
calls = []


def fake_rescue(q, materials, cfg, picks=None, on_delta=None, kind=""):
    calls.append(q)
    time.sleep(0.05)
    for c in FAKE:
        on_delta(c)
    return "".join(FAKE)


app.llm_rescue = fake_rescue
cfg = app.load_config()
mats = app.load_materials()
root = tk.Tk()
root.withdraw()
a = app.CueApp(root, cfg, mats)


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def body():
    return a.txt.get("1.0", "end").strip()


ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✓ %s" % name)
    else:
        fail += 1
        print("  ✗ %s  %s" % (name, extra))


print("1) 技术题：回车即出 AI 答案")
a.answer_mode = True                      # 开答案库模式也要走 AI
for q in ["接口请求方式有几种", "TCP 和 UDP 有什么区别", "什么是幂等"]:
    a.q_var.set(q)
    a._ph_on = False
    a.generate()
    pump(1.2)
    check("%s -> AI 作答" % q, body().startswith("▍ AI 作答"), body()[:40])
    check("  没有认输话术", "我不会" not in body() and "没做过" not in body())

print("2) 经历题：走素材/草稿，不调模型")
for q in ["为什么从上一家公司离职", "讲讲你最有挑战的项目"]:
    n = len(calls)
    a.q_var.set(q)
    a._ph_on = False
    a.generate()
    pump(0.4)
    head = body().splitlines()[0]
    check("%s -> 素材/草稿" % q, not head.startswith("▍ AI 作答"), head)
    check("  素材盖得住，不烧 token", len(calls) == n, "%d -> %d" % (n, len(calls)))

# 素材完全没有的题（如"你最大的缺点"）：界面仍给兜底卡，后台顺带预热一份，
# 这是设计如此，不算误调——只确认它没有直接顶掉界面内容。
a.q_var.set("你最大的缺点是什么")
a._ph_on = False
a.generate()
pump(0.4)
check("无素材的个人题 -> 仍是兜底卡", not body().startswith("▍ AI 作答"),
      body().splitlines()[0])

print("3) 答案库：0 分素材不许念稿")
ans = app.load_answers()
bad = 0
for q in ["你最大的缺点是什么", "你最大的优势是什么"]:
    _, _, picks, _ = app.build_local_cue(q, mats)
    txt, _ = app.build_answer(q, ans, picks)
    if txt:
        bad += 1
        print("   ✗ %s 仍返回「%s」" % (q, txt.splitlines()[2][:20]))
check("不相关题不再返回错稿", bad == 0)

print("4) 137 条标题仍能出草稿")
miss = []
for m in mats:
    t = app.material_title(m)
    if not t:
        continue
    _, _, picks, _ = app.build_local_cue(t, mats)
    txt, _ = app.build_answer(t, ans, picks)
    if not txt:
        miss.append(t)
check("137 条全覆盖（缺 %d 条）" % len(miss), not miss, str(miss[:3]))

root.destroy()
print("\n%s  通过 %d ｜ 失败 %d" % ("ALL PASS" if not fail else "有失败", ok, fail))
raise SystemExit(1 if fail else 0)
