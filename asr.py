# -*- coding: utf-8 -*-
"""
面试提词器 · v0.2 收音模块

链路：WASAPI 回环（系统声音）→ 降采样 16k 单声道 → 能量 VAD 切句
      → faster-whisper 整句转写 → 问句闸门 → 回调给 UI

设计取舍
- 不做流式：等对方说完（静音 700ms）再整句识别，稳定、延迟可预期，工程复杂度减半
- 只录系统输出（对方声音），不碰麦克风，所以自己说话不会触发自己
- 全程本地，音频只在内存里转一圈，不落盘、不联网
"""

import queue
import threading
import time

import numpy as np

TARGET_SR = 16000
FRAME_MS = 30
HOP = TARGET_SR * FRAME_MS // 1000  # 480 采样

# 问句判据：句尾语气词，或句中疑问/指令词
Q_TAIL = ("吗", "呢", "么")
Q_WORDS = (
    "什么", "怎么", "为什么", "如何", "哪", "谁", "多少", "几个", "多久", "多大",
    "有没有", "是不是", "能不能", "会不会", "要不要", "可不可以",
    "介绍", "说说", "讲讲", "聊聊", "谈谈", "举例", "举个例子", "区别", "对比",
    "了解", "看法", "评价", "经历", "经验", "遇到过", "处理过", "做过",
)


def is_question(text):
    """判断这句话值不值得弹提词：拦住闲聊、口头禅、附和"""
    s = (text or "").strip().strip("。，,.!！?？ ")
    if len(s) < 4:
        return False
    if s.endswith(Q_TAIL):
        return True
    if any(w in s for w in Q_WORDS):
        return True
    return False


def looks_like_garbage(text, prompt=""):
    """判这段文本是不是"对着噪声编出来的"。

    whisper 在静音/噪声上会稳定产出三类垃圾：
      1) 复读 initial_prompt（"这些是普通话面试问答"）
      2) 单字/双字无限重复（"许多,许多,许多…"）
      3) 真正的字幕语（"谢谢观看""请不吝点赞"）
    这里用字符重复率 + 与提示词的重合度判，比只看 no_speech_prob 稳。
    """
    import re as _re
    from collections import Counter
    t = _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text or "")
    if len(t) < 2:
        return True
    if len(t) < 3:                       # 一两个字（"嗯""的"）没有信息量
        return True
    # 单字重复率（短句天然重复度高，只在长文本上判）
    if len(t) >= 8:
        c = Counter(t)
        if c.most_common(1)[0][1] / len(t) > 0.45:
            return True
    # 二元组重复率（"许多,许多,许多…"就是这个形态）
    if len(t) >= 12:
        bgs = [t[i:i + 2] for i in range(len(t) - 1)]
        cb = Counter(bgs)
        if cb.most_common(1)[0][1] / len(bgs) > 0.40:
            return True
    # 复读提示词（噪声幻觉最常见的形态）
    if prompt:
        p = _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", prompt)
        if len(t) >= 4 and t in p:
            return True
        if p and _lcs(t, p) / len(t) > 0.6:
            return True
    return False


def _lcs(a, b):
    """最长公共子串长度"""
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


# Whisper 中文经常吐繁体，素材库是简体，不转就检索全空
_CC = None


def to_simplified(text):
    global _CC
    if not text:
        return text
    if _CC is None:
        try:
            import opencc
            _CC = opencc.OpenCC("t2s")
        except Exception:
            _CC = False
    return _CC.convert(text) if _CC else text


def to_16k(x, sr):
    """降采样到 16k 单声道。整除走均值抽取（自带低通），否则线性插值"""
    if sr == TARGET_SR:
        return x.astype(np.float32, copy=False)
    if sr % TARGET_SR == 0:
        k = sr // TARGET_SR
        n = len(x) - len(x) % k
        return x[:n].reshape(-1, k).mean(axis=1).astype(np.float32)
    n_out = max(1, int(len(x) * TARGET_SR / sr))
    idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


# ------------------------------------------------------------------ 采集

class AudioSource:
    """打开一个输入设备并持续读帧，回调 mono 16k float32。

    mode = "loopback" 录系统输出（视频面试：对方的声音）
    mode = "mic"      录麦克风（电话/线下：双方声音混在一起）
    """

    def __init__(self, mode="loopback", on_frame=None, on_error=None, device_index=-1):
        self.mode = mode
        self.device_index = device_index
        self.on_frame = on_frame or (lambda a: None)
        self.on_error = on_error or (lambda m: None)
        self._pa = None
        self._stream = None
        self._thread = None
        self._running = False
        self.device_name = ""
        self.samplerate = 0

    # -- 找设备
    def _pick(self, p):
        import pyaudiowpatch as pa
        if self.device_index is not None and self.device_index >= 0:
            d = p.get_device_info_by_index(self.device_index)
            return d
        if self.mode == "mic":
            d = p.get_default_input_device_info()
            return d
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        try:
            for d in p.get_loopback_device_info_generator():
                if out["name"] in d["name"]:
                    return d
        except Exception:
            pass
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d.get("isLoopbackDevice") and out["name"] in d["name"]:
                return d
        return None

    def open(self):
        import pyaudiowpatch as pa
        self._pa = pa.PyAudio()
        dev = self._pick(self._pa)
        if dev is None:
            raise RuntimeError("找不到回环设备（loopback），请确认系统有可用扬声器")
        self.device_name = dev["name"]
        self.samplerate = int(dev["defaultSampleRate"])
        ch = max(1, min(2, int(dev["maxInputChannels"])))
        self._ch = ch
        self._stream = self._pa.open(
            format=pa.paFloat32, channels=ch, rate=self.samplerate,
            input=True, input_device_index=dev["index"], frames_per_buffer=1024)
        self._stream.start_stream()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        sr = self.samplerate
        t0 = time.monotonic()
        frames = 0
        while self._running:
            try:
                data = self._stream.read(1024, exception_on_overflow=False)
            except Exception as e:
                if self._running:
                    self.on_error("读音频失败：%s" % e)
                break
            arr = np.frombuffer(data, dtype=np.float32)
            if self._ch > 1:
                arr = arr.reshape(-1, self._ch).mean(axis=1)
            frames += len(arr)
            # 静音时 WASAPI 回环返回得比实时快，会空转吃满一个核，这里做节流
            ahead = frames / sr - (time.monotonic() - t0)
            if ahead > 0.02:
                time.sleep(min(ahead, 0.05))
            try:
                self.on_frame(to_16k(arr, sr))
            except Exception as e:
                self.on_error("处理音频失败：%s" % e)

    def close(self):
        self._running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0)
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._pa:
                self._pa.terminate()
        except Exception:
            pass
        self._stream = None
        self._pa = None


# ------------------------------------------------------------------ 切句

class Segmenter:
    """能量 VAD：自适应底噪 + 峰值跟踪 + 预录缓冲 + 尾部静音断句

    微信语音 / 会议软件出来的声音有两个特点：整体音量低、压缩失真让句中能量忽高忽低。
    实测同一句人声衰减 26dB 后帧 RMS 峰值只剩 0.011（90 分位 0.005），
    旧阈值 0.006 只覆盖 16% 的帧——只有最响那几个字"听得见"，句子自然拼不全。

    所以这里不跟绝对音量较劲：
    - 用 60ms 包络平滑判"在不在说话"，躲开词间瞬间低谷把句子切碎
    - 阈值 = 底噪 × 3（下限 0.0006），底噪在静音时慢慢跟上来，说话时不动
    - 段落时长 ≥ min_speech_ms 才算一句，短碎片攒着拼到下一句，不丢
    """

    ABS_FLOOR = 0.0006     # 绝对下限：再低就是 16bit 量化噪声
    ABS_CEIL = 0.02        # 绝对上限：底噪被空调/风扇顶高时的保护
    ENV_A = 0.35           # 包络平滑系数（约 60ms）

    def __init__(self, on_segment, silence_ms=750, min_speech_ms=300,
                 max_seg_ms=15000, preroll_ms=500):
        self.on_segment = on_segment
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.max_seg_ms = max_seg_ms
        self.preroll = int(TARGET_SR * preroll_ms / 1000)
        self.noise = 0.0008    # 自适应底噪（慢速跟踪）
        self._env = 0.0        # 包络
        # 启动校准：前 1 秒只量底噪不判说话。否则环境本来就吵（风扇/空调）时，
        # 噪声一进来就越过初始阈值，而底噪只有在"静音"时才更新 → 死锁，永远在说话状态
        self._cal = []
        self._calibrated = False
        self._carry = np.zeros(0, dtype=np.float32)   # 太短没成句的碎片，攒着拼下一句
        self.last_rms = 0.0
        self.threshold = self.ABS_FLOOR
        self.speaking = False  # 给 UI 判断"正在说话"
        self.reset()

    def reset(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._pre = np.zeros(0, dtype=np.float32)
        self._seg = []
        self._sil = 0
        self._loud_run = 0
        self.speaking = False

    def push(self, frame):
        self._buf = np.concatenate([self._buf, frame]) if len(self._buf) else frame
        while len(self._buf) >= HOP:
            hop = self._buf[:HOP]
            self._buf = self._buf[HOP:]
            self._step(hop)

    def _thr(self):
        return max(min(self.noise * 3.0, self.ABS_CEIL), self.ABS_FLOOR)

    def note_noise(self, audio):
        """这一段没识别出人声 → 当成底噪，抬高阈值，别在同一片噪声上来回切"""
        if len(audio) < TARGET_SR * 0.5:
            return
        e = float(np.sqrt(np.mean(np.asarray(audio) ** 2)))
        self.noise = max(self.noise, min(e * 0.7, self.ABS_CEIL / 3.0))

    def _step(self, hop):
        rms = float(np.sqrt(np.mean(hop ** 2)))
        self.last_rms = rms
        # 包络平滑：单帧 RMS 受爆破音/词间间隙影响太大，平滑后再判
        self._env = (1 - self.ENV_A) * self._env + self.ENV_A * rms

        if not self._calibrated:
            self._cal.append(rms)
            if len(self._cal) >= 33:                  # 约 1 秒
                self.noise = max(1e-5, float(np.percentile(self._cal, 30)))
                self._calibrated = True
            self._pre = np.concatenate([self._pre, hop])[-self.preroll:]
            return

        thr = self._thr()
        self.threshold = thr
        loud = self._env > thr

        if not self.speaking:
            if not loud:
                self.noise = 0.98 * self.noise + 0.02 * max(self._env, 1e-5)
                self._loud_run = 0
                self._pre = np.concatenate([self._pre, hop])[-self.preroll:]
                return
            self._loud_run += 1
            self._pre = np.concatenate([self._pre, hop])[-self.preroll:]
            if self._loud_run >= 2:          # 连续两帧算真开口，躲开键盘声
                self.speaking = True
                pre = self._pre
                if len(self._carry):
                    pre = np.concatenate([self._carry, pre])
                    self._carry = np.zeros(0, dtype=np.float32)
                self._seg = [pre]
                self._sil = 0
            return

        self._seg.append(hop)
        if loud:
            self._sil = 0
        else:
            self._sil += FRAME_MS

        too_long = len(self._seg) * HOP >= TARGET_SR * self.max_seg_ms / 1000
        if self._sil >= self.silence_ms or too_long:
            audio = np.concatenate(self._seg)
            # 掐掉尾部多余静音，只留 200ms
            keep = len(audio) - int(TARGET_SR * max(0, self._sil - 200) / 1000)
            audio = audio[:max(keep, int(TARGET_SR * 0.3))]
            dur_ms = int(1000 * len(audio) / TARGET_SR)
            self.reset()
            if dur_ms >= self.min_speech_ms:
                self.on_segment(audio, dur_ms)
            else:
                # 半截话不扔：留 600ms 尾巴，下次开口接上去，否则短问句永远收不到
                self._carry = audio[-int(TARGET_SR * 0.6):]


# ------------------------------------------------------------------ 识别

class Transcriber:
    """faster-whisper，CPU int8。模型只在需要时加载。"""

    def __init__(self, model_dir, cpu_threads=8):
        from faster_whisper import WhisperModel  # 延迟导入，缺依赖时 UI 仍能跑
        self.model_dir = str(model_dir)
        self.model = WhisperModel(self.model_dir, device="cpu",
                                  compute_type="int8", cpu_threads=cpu_threads)
        self.last_meta = {}

    def transcribe(self, audio, hotwords=""):
        t0 = time.time()
        # 提示词用简体，能明显抑制繁体输出和漏字
        prompt = hotwords or "以下是普通话面试问答，简体中文。"
        # 微信语音/会议软件的音量常常只有正常语音的 1/10，先拉到 -1dB 再识别；
        # 增益封顶 8 倍，免得把底噪也放大成"人声"
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        gain = 1.0
        if peak > 1e-4:
            gain = min(8.0, 0.9 / peak)
            audio = audio * gain
        segs, _info = self.model.transcribe(
            audio, language="zh", beam_size=1, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=True,
            temperature=0.0, initial_prompt=prompt,
        )
        # 对着噪声，whisper 会一本正经编出「谢谢观看」「请不吝点赞」这类字幕语。
        # no_speech_prob 高 + 置信度低，两个条件同时满足才丢，避免误杀小声真语音
        parts, dropped = [], 0
        for s in segs:
            nsp = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
            alp = float(getattr(s, "avg_logprob", 0.0) or 0.0)
            if nsp > 0.6 and alp < -0.5:
                dropped += 1
                continue
            if looks_like_garbage(s.text, prompt):
                dropped += 1
                continue
            parts.append(s.text)
        text = to_simplified("".join(parts).strip())
        if text and looks_like_garbage(text, prompt):
            dropped += 1
            text = ""
        self.last_meta = {"peak": round(peak, 4), "gain": round(gain, 1), "dropped": dropped}
        return text, (time.time() - t0) * 1000


class AsrService:
    """把采集、切句、识别串起来，用回调驱动 UI。

    回调：
      on_status(state, detail)  state ∈ loading/listening/speech/transcribing/off/error
      on_level(rms, speaking)   约每帧一次（33ms），给电平条用
      on_text(text, meta)       识别出一句「像问题」的话
    """

    def __init__(self, model_dir, on_text=None, on_status=None, on_level=None,
                 mode="loopback", silence_ms=750, hotwords="", cpu_threads=8,
                 device_index=-1):
        self.model_dir = model_dir
        self.on_text = on_text or (lambda t, m: None)
        self.on_status = on_status or (lambda s, d="": None)
        self.on_level = on_level or (lambda r, s: None)
        self.mode = mode
        self.silence_ms = silence_ms
        self.hotwords = hotwords
        self.cpu_threads = cpu_threads
        self.device_index = device_index

        self._src = None
        self._seg = None
        self._q = queue.Queue(maxsize=4)
        self._worker = None
        self._running = False
        self._tr = None
        self._last_text = ""
        self._last_time = 0.0
        self.stats = {"segments": 0, "questions": 0, "asr_ms": []}

    # -- 生命周期
    def start(self):
        if self._running:
            return
        self._running = True
        self.on_status("loading", "加载模型…")
        self._src = AudioSource(self.mode, on_frame=self._on_frame,
                                on_error=lambda m: self.on_status("error", m),
                                device_index=self.device_index).open()
        self._seg = Segmenter(self._on_segment, silence_ms=self.silence_ms)
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._running = False
        if self._src:
            self._src.close()
            self._src = None
        self.on_status("off", "")

    def switch_mode(self, mode):
        self.mode = mode
        if self._running:
            self.stop()
            self.start()

    # -- 内部
    def _on_frame(self, frame):
        if self._seg:
            self._seg.push(frame)
            self.on_level(self._seg.last_rms, self._seg.speaking)

    def _on_segment(self, audio, speech_ms):
        self.stats["segments"] += 1
        if self._q.full():
            try:
                self._q.get_nowait()   # 丢最旧的，宁可漏一句也不积压
            except Exception:
                pass
        self._q.put((audio, speech_ms))

    def _loop(self):
        # 先把模型加载好，再开始消费切句结果（首句不必多等 2 秒）
        try:
            self._tr = Transcriber(self.model_dir, self.cpu_threads)
            self.on_status("listening", "")
        except Exception as e:
            self.on_status("error", "模型加载失败：%s" % e)
            return
        while self._running:
            try:
                audio, speech_ms = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self._running:
                break
            try:
                self.on_status("transcribing", "")
                text, ms = self._tr.transcribe(audio, self.hotwords)
                self.stats["asr_ms"].append(ms)
                self.on_status("listening", "")
                if not text:
                    if self._seg:
                        self._seg.note_noise(audio)
                    continue
                if not is_question(text):
                    self.on_text("", dict(raw=text, skipped=True, asr_ms=ms,
                                          speech_ms=speech_ms, **self._tr.last_meta))
                    continue
                now = time.time()
                if text == self._last_text and now - self._last_time < 5:
                    continue
                self._last_text, self._last_time = text, now
                self.stats["questions"] += 1
                self.on_text(text, dict(raw=text, asr_ms=ms, speech_ms=speech_ms,
                                        **self._tr.last_meta))
            except Exception as e:
                self.on_status("error", "识别失败：%s" % e)

    def avg_asr_ms(self):
        v = self.stats["asr_ms"]
        return int(sum(v) / len(v)) if v else 0


# ------------------------------------------------------------------ CLI 自测

def _cli():
    import sys
    from pathlib import Path

    if "--devices" in sys.argv:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d.get("isLoopbackDevice") or d["maxInputChannels"] > 0:
                print("[%2d] in=%d %s%s" % (i, d["maxInputChannels"], d["name"],
                                            "  <loopback>" if d.get("isLoopbackDevice") else ""))
        p.terminate()
        return

    secs = 20
    if "--listen" in sys.argv:
        try:
            secs = float(sys.argv[sys.argv.index("--listen") + 1])
        except Exception:
            pass

    base = Path(__file__).resolve().parent
    model = base / "models" / "faster-whisper-small"
    if not (model / "model.bin").exists():
        print("模型不存在：%s" % model)
        return

    mark = time.time()
    diag = "--diag" in sys.argv
    if diag:
        secs = 30
        if "--diag" in sys.argv:
            try:
                secs = float(sys.argv[sys.argv.index("--diag") + 1])
            except Exception:
                pass

    def db(v):
        return 20 * np.log10(max(v, 1e-6))

    def cb_text(t, meta):
        seg = svc._seg if svc else None
        info = "峰值 %.0fdB 增益 %.1fx 阈值 %.0fdB" % (
            db(meta.get("peak", 0)), meta.get("gain", 1.0),
            db(seg.threshold if seg else 0))
        if meta.get("dropped"):
            info += " ｜ 丢掉%d段低置信" % meta["dropped"]
        if meta.get("skipped"):
            print("  [非问句] %s\n            %s" % (meta["raw"] or "（空）", info))
        elif t:
            print("\n>>> %s\n    识别 %.0fms ｜ 语音 %.1fs ｜ %s\n"
                  % (t, meta["asr_ms"], meta["speech_ms"] / 1000, info))

    def cb_status(s, d=""):
        if s in ("error", "loading"):
            print("[状态] %s %s" % (s, d))

    svc = AsrService(model, on_text=cb_text, on_status=cb_status)
    svc.start()
    if diag:
        print("开始收音 %.0f 秒 ｜ 电平每秒打一行，放一段语音看数字\n"
              "  峰值 < -45dB：几乎没声音（设备/音量问题）\n"
              "  -45 ~ -30dB：小声，能认但容易错\n"
              "  > -25dB：正常音量\n" % secs)
    else:
        print("开始收音 %.0f 秒，说几句中文试试（Ctrl-C 结束）…" % secs)

    t_end = time.time() + secs
    last_beat = 0.0
    try:
        while time.time() < t_end:
            time.sleep(0.25)
            if diag and time.time() - last_beat > 1.0:
                last_beat = time.time()
                seg = svc._seg
                if seg:
                    print("   [%4.0fs] 峰值 %5.0fdB ｜ 阈值 %5.0fdB ｜ 底噪 %5.0fdB ｜ %s"
                          % (time.time() - mark, db(seg.last_rms), db(seg.threshold),
                             db(seg.noise), "说话中" if seg.speaking else "静音"))
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
        print("\n切句 %d 次（其中问句 %d）｜ 识别均值 %dms"
              % (svc.stats["segments"], svc.stats["questions"], svc.avg_asr_ms()))


if __name__ == "__main__":
    _cli()
