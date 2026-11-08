# -*- coding: utf-8 -*-
"""收音回归测试：不用麦克风、不用联网，验证 VAD 切句与识别准确率。

跑法：python test_asr.py
覆盖这几件事（都是真实踩过的坑）：
  1. 小音量（微信语音那种）能不能切出完整句
  2. 句中停顿 350ms 不该切、900ms 该切
  3. 被压缩失真切成碎片时，碎片要拼回去而不是丢掉
  4. 环境有恒定底噪时，不能一直误触发
  5. -26dB 小音量识别出来的字错率应该接近 0
  6. 纯噪声不能吐出"许多许多许多"这类幻觉
  7. 正常的短问句不能被过滤器误杀
"""
import re
import sys
import wave

import numpy as np

import asr

HOT = ("以下是普通话面试问答。专有名词：Jenkins、JMeter、MySQL、Redis、Kafka、"
       "Docker、Linux、Shell、Python、Selenium、Postman、TPS、QPS、ERP、BOM、SQL。")
ORIG = ["你好，先简单介绍一下你自己吧。", "你做过最有挑战的项目是什么，难点在哪里？",
        "MySQL 的慢查询你是怎么优化的？", "TCP 和 UDP 有什么区别？", "为什么从上一家公司离职？"]

OK, FAIL = [], []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print("  %s %-34s %s" % ("✓" if cond else "✗", name, detail))


def load(p):
    with wave.open(str(p), "rb") as w:
        sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return asr.to_16k(x, sr)


def silence(ms):
    return np.zeros(int(asr.TARGET_SR * ms / 1000), dtype=np.float32)


def feed(seg, audio):
    for i in range(0, len(audio) - asr.HOP + 1, asr.HOP):
        seg.push(audio[i:i + asr.HOP])


def segments(audio, silence_ms=750):
    got = []
    seg = asr.Segmenter(lambda a, ms: got.append(ms), silence_ms=silence_ms)
    feed(seg, np.concatenate([silence(1200), audio, silence(1500)]))
    return got


def norm(s):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "").lower()


def cer(ref, hyp):
    a, b = norm(ref), norm(hyp)
    d = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(b) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(b)] / len(a)


def main():
    import os
    if not os.path.isdir("_test_audio"):
        print("缺 _test_audio/，先跑 python test_e2e.py 生成测试语音")
        return 1
    voice = load("_test_audio/q0.wav")
    quiet = voice * 10 ** (-26 / 20)          # 衰减到微信语音那种音量
    print("测试音频 %.1fs，小音量版峰值 %.3f\n" % (len(voice) / 16000, float(np.abs(quiet).max())))

    print("[切句]")
    g = segments(quiet)
    check("小音量整句切出 1 段", len(g) == 1, "%d 段" % len(g))
    check("段长接近原句", g and g[0] > 2500, "%dms" % (g[0] if g else 0))

    half = len(quiet) // 2
    g = segments(np.concatenate([quiet[:half], silence(350), quiet[half:]]))
    check("停 350ms 不切断", len(g) == 1, "%d 段" % len(g))
    g = segments(np.concatenate([quiet[:half], silence(900), quiet[half:]]))
    check("停 900ms 正常断句", len(g) == 2, "%d 段" % len(g))

    frag = np.concatenate([np.concatenate([quiet[i * 1600:(i + 1) * 1600], silence(300)])
                           for i in range(6)])
    g = segments(np.concatenate([frag, silence(1000)]))
    check("碎片合并成 1 段", len(g) == 1, "%d 段" % len(g))

    noise = np.random.normal(0, 0.004, 16000 * 5).astype(np.float32)
    got = []
    seg = asr.Segmenter(lambda a, ms: got.append(ms), silence_ms=750)
    feed(seg, np.concatenate([noise[:24000], noise]))
    check("恒定底噪不反复触发", len(got) <= 1, "%d 段" % len(got))

    print("\n[识别]")
    tr = asr.Transcriber("models/faster-whisper-small", 8)
    errs = []
    for ref, i in zip(ORIG, range(5)):
        txt, _ = tr.transcribe(load("_test_audio/q%d.wav" % i) * 10 ** (-26 / 20), HOT)
        errs.append(cer(ref, txt))
    avg = 100 * sum(errs) / len(errs)
    check("-26dB 小音量字错率 < 10%", avg < 10, "CER %.1f%%" % avg)

    z = np.random.normal(0, 0.004, 16000 * 3).astype(np.float32)
    txt, _ = tr.transcribe(z, HOT)
    check("纯噪声不产生幻觉文本", txt == "", repr(txt[:20]))

    print("\n[过滤器边界]")
    for s, want_drop in [("还有吗", False), ("讲讲你的项目", False),
                         ("这些是普通话面试问答", True),
                         ("许多,许多,许多,许多,许多,许多,许多", True)]:
        got_drop = asr.looks_like_garbage(s, HOT)
        check("「%s」%s" % (s[:10], "应丢弃" if want_drop else "应保留"), got_drop == want_drop)

    print("\n%s  通过 %d ｜ 失败 %d" % ("ALL PASS" if not FAIL else "有失败", len(OK), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
