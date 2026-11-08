# -*- coding: utf-8 -*-
"""pyaudiowpatch WASAPI loopback 实测：边播声音边录系统输出，验证能拿到波形。"""
import threading, time, math, struct, wave, os, tempfile
import numpy as np
import pyaudiowpatch as pa

def make_wav(path, sec=2.0, sr=44100, freq=440):
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        frames = bytearray()
        for i in range(int(sr * sec)):
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr)))
        w.writeframes(bytes(frames))

def main():
    p = pa.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        print("默认输出:", default_out["name"])
        if not default_out["isLoopbackDevice"]:
            lb = None
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                if d.get("isLoopbackDevice") and default_out["name"] in d["name"]:
                    lb = d; break
            if lb is None:
                print("!! 找不到 loopback 设备"); return
            default_out = lb
        print("loopback 设备:", default_out["name"], "| ch", default_out["maxInputChannels"],
              "| sr", int(default_out["defaultSampleRate"]))

        sr = int(default_out["defaultSampleRate"]); ch = min(2, default_out["maxInputChannels"])
        st = p.open(format=pa.paFloat32, channels=ch, rate=sr,
                    input=True, input_device_index=default_out["index"], frames_per_buffer=1024)
        st.start_stream()

        wavp = os.path.join(tempfile.gettempdir(), "_tone.wav")
        make_wav(wavp)
        def beep():
            time.sleep(0.4)
            import winsound
            winsound.PlaySound(wavp, winsound.SND_FILENAME)
        threading.Thread(target=beep, daemon=True).start()

        chunks = []
        t0 = time.time()
        while time.time() - t0 < 3.0:
            data = st.read(1024, exception_on_overflow=False)
            chunks.append(np.frombuffer(data, dtype=np.float32))
        st.stop_stream(); st.close()

        audio = np.concatenate(chunks)
        rms = float(np.sqrt((audio ** 2).mean()))
        peak = float(np.abs(audio).max())
        # 找出有声段占比
        win = sr // 10
        seg = [float(np.sqrt((audio[i:i+win] ** 2).mean())) for i in range(0, len(audio) - win, win)]
        loud = sum(1 for s in seg if s > 1e-3)
        print("录制 %.1f 秒  samples=%d  peak=%.4f  rms=%.5f" % (3.0, len(audio), peak, rms))
        print("有声窗口(>0.001): %d / %d  → %s" % (loud, len(seg), "LOOPBACK 可用" if loud > 0 else "全静音"))
    finally:
        p.terminate()

if __name__ == "__main__":
    main()
