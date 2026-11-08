# -*- coding: utf-8 -*-
"""端到端测试：SAPI 合成中文问题 -> 扬声器播放 -> 回环采集 -> 识别 -> 出提纲

测的是"说完到出提示"的真实延迟，不是纯推理耗时。
"""
import sys
import time
import winsound
from pathlib import Path

import asr

BASE = Path(__file__).resolve().parent
MODEL = BASE / "models" / "faster-whisper-small"
AUDIO = BASE / "_test_audio"

QUESTIONS = [
    "你好，先简单介绍一下你自己吧。",
    "你做过最有挑战的项目是什么，难点在哪里？",
    "MySQL 的慢查询你是怎么优化的？",
    "TCP 和 UDP 有什么区别？",
    "为什么从上一家公司离职？",
]


def synth(text, path):
    import win32com.client as w
    v = w.Dispatch("SAPI.SpVoice")
    for x in v.GetVoices():
        if "Huihui" in x.GetDescription():
            v.Voice = x
            break
    fs = w.Dispatch("SAPI.SpFileStream")
    fs.Open(str(path), 3, False)
    v.AudioOutputStream = fs
    v.Speak(text)
    fs.Close()


def main():
    AUDIO.mkdir(exist_ok=True)
    for i, q in enumerate(QUESTIONS):
        p = AUDIO / ("q%d.wav" % i)
        if not p.exists():
            synth(q, p)
    print("测试音频 %d 条 -> %s" % (len(QUESTIONS), AUDIO))

    hits = []

    def on_text(t, meta):
        if t:
            hits.append((t, meta, time.time()))
        else:
            print("    [未成问句] %s" % meta.get("raw", ""))

    def on_status(s, d=""):
        if s in ("error", "loading"):
            print("  [状态] %s %s" % (s, d))

    svc = asr.AsrService(MODEL, on_text=on_text, on_status=on_status)
    svc.start()
    print("等模型加载…")
    for _ in range(60):
        if svc._tr is not None:
            break
        time.sleep(0.2)
    orig_seg = svc._seg.on_segment

    def spy(audio, ms):
        print("    [切句] 语音 %.1fs" % (ms / 1000.0))
        orig_seg(audio, ms)

    svc._seg.on_segment = spy
    print("模型就绪（%.1fs 后开始播放）\n" % 1.0)
    time.sleep(1.0)

    rows = []
    for i, q in enumerate(QUESTIONS):
        hits.clear()
        winsound.PlaySound(str(AUDIO / ("q%d.wav" % i)), winsound.SND_FILENAME)
        t_end = time.time()
        for _ in range(80):
            if hits:
                break
            time.sleep(0.05)
        if hits:
            t, meta, ts = hits[0]
            rows.append((q, t, ts - t_end, meta.get("asr_ms", 0), meta.get("speech_ms", 0)))
            print("Q%d 原句：%s" % (i + 1, q))
            print("    识别：%s" % t)
            print("    说完→回调 %.2fs ｜ 识别 %.0fms ｜ 语音 %.1fs\n"
                  % (ts - t_end, meta.get("asr_ms", 0), meta.get("speech_ms", 0) / 1000))
        else:
            print("Q%d 原句：%s\n    ✗ 未触发提词\n" % (i + 1, q))
        time.sleep(1.2)

    svc.stop()
    ok = len(rows)
    print("=" * 52)
    print("触发 %d/%d" % (ok, len(QUESTIONS)))
    if rows:
        lat = [r[2] for r in rows]
        ms = [r[3] for r in rows]
        print("延迟 平均 %.2fs ｜ 最大 %.2fs" % (sum(lat) / len(lat), max(lat)))
        print("识别 平均 %.0fms" % (sum(ms) / len(ms)))


if __name__ == "__main__":
    main()
