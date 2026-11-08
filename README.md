# 面试提词器（桌面版）

单人自用的实时面试提词窗：**面试官说完问题，约 1.7 秒后屏幕上弹出提纲 + 你自己的真实素材。**

不是题库软件，也不给逐字稿——它做的是「把手边素材立刻调出来」，让你说自己的东西，而不是背模板。

```
┌────────────────────────────────────────┐
│ 面试提词器            AI 待命   ● 收音中 │
├────────────────────────────────────────┤
│ 你怎么理解全链路压测                      │
│                                        │
│ ▍ 性能测试 ｜ 直接答                      │
│  · 1000 并发下单，P99 从 900ms 降到 180ms │
│  · JMeter 参数化 token 存 csv，CSV 数据源 │
│  · 瓶颈定位：先看 CPU 还是 IO，再拆链路    │
│  💡 素材原文：ERP 大促压测，QPS 800→3200  │
├────────────────────────────────────────┤
│ Enter 生成 · ^Enter AI兜底 · Esc 收起     │
└────────────────────────────────────────┘
```

## 三个能力

| 能力 | 说明 |
|---|---|
| **自动收音** | WASAPI 回环录系统声音（线上视频面试不认识麦克风也能收），能量 VAD 自动切句，faster-whisper `small` 本地转写，1G 内存可跑。小音量（微信语音那种）也做了自动增益 |
| **本地检索** | 把 `data/resume.md` 里的素材做成可检索条目，主题路由 + IDF 加权 + 标题覆盖率打分，命中/接近/未命中三档。**全程离线，0.2 秒内出结果** |
| **AI 兜底** | 库里没有的题，接 DeepSeek（任意 OpenAI 兼容端点）现场作答。技术题直接给「答 / 深 / 例」三段口语答案；经历题给「接 / 移 / 收」。**听到题就在后台预生成**，按键时多数情况已经备好 |

## 快速开始

```bash
pip install -r requirements.txt
cp data/resume.example.md data/resume.md     # 然后改成你自己的素材
python app.py
```

首次运行会自动下载 faster-whisper `small` 模型（约 461MB，存到 `models/`）。只想要手动模式的话，把 `data/config.json` 里 `asr.enabled` 设成 `false`。

### 配 AI（可选）

`data/config.json` 里的 `llm.api_key` 留空，真实 key 放**本地文件** `data/config.local.json`（已 gitignore）：

```json
{ "llm": { "api_key": "sk-你的key" } }
```

或者直接用环境变量：

```bat
set INTERVIEW_LLM_KEY=sk-你的key
```

优先级：环境变量 > `config.local.json` > `config.json`。`llm.enabled` 控制要不要实时提纲（默认关，走本地检索）。

### 快捷键

| 键 | 作用 |
|---|---|
| `Enter` | 出内容（技术题直接出 AI 答案） |
| `Ctrl+Enter` | 经历题兜底 / 再按一次要更细的版本 |
| `F5` | 重载素材和答案库 |
| `F7` | 要点 / 答案草稿 切换 |
| `F8` | 开关收音 |
| `F9` | 应急隐身（缩成 88×10 一条，屏幕共享时用） |
| `Esc` | 收起 / 展开 |

## 屏幕共享

窗口默认调用 `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`，**共享屏幕、录屏、截图都不会拍到它**（需 Win10 2004+）。F9 是兜底方案。

## 目录

```
app.py                 主程序（界面 + 检索 + 提示词）
asr.py                 收音（回环采集 + VAD + whisper）
gen_answers.py         把素材批量预生成口语草稿（调模型）
测速.py                OpenAI 兼容端点首字延迟实测，换模型前先量
data/resume.md         你的素材库（gitignore，从 example 复制）
data/answers.md        预生成答案草稿（gitignore）
data/config.json       配置
test_asr.py            收音回归 12 项
test_answer.py         作答回归 13 项
test_e2e.py            端到端 5 题
```

自测：

```bash
python app.py --selftest      # GUI 自检 + 检索回归
python test_asr.py            # 收音（切句 / 小音量 / 噪声过滤）
python test_answer.py         # 作答（技术题 / 经历题 / 错稿拦截）
python asr.py --diag 20       # 诊断：每秒打电平 dBFS，看声音进没进来
```

## 已知局限

- 检索是字面匹配，没有语义模型。「全链路压测」和「1000 并发下单」这种零字面重合的会判成未命中，走 AI 兜底。
- AI 生成的技术答案只是通用理解，很偏的题（手写红黑树之类）照着说有六七成，追问细节还得靠自己的底子。
- 收音依赖「电脑出声」。手机 / 蓝牙耳机播的声音，回环收不到。

## 合规提醒

多数公司禁止面试中使用外部辅助工具。这个项目定位是**模拟练习、赛后复盘、允许开卷的场景**。是否在真实面试中使用，由你判断并承担后果。防截屏只是技术手段，不等于合规。

## License

MIT
