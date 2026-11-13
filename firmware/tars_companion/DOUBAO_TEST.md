# TARS 豆包端到端音色测试

记录日期：2026-11-08。当前交付为可审阅实现及离线测试通过版本，尚未进行真实豆包调用或机器人试听。所有新增文件位于本 G 盘工程；未修改、编译或烧写固件。保留电脑 Companion 中转，最终由机器人语音模块播放。

## 已确认的现有架构

| 环节 | 当前实现 |
| --- | --- |
| 麦克风 | ESP32 I2S0 RX，GPIO14/13/12；16 kHz PCM16 LE 单声道 |
| 传输 | ESP32 主动连接电脑；TWA1 认证 TCP；每块 ACK、心跳、序号校验；只适用于可信局域网 |
| ASR | Companion 调用 DashScope `fun-asr-realtime` 云服务 |
| 对话 | Companion 调用选定 provider；现有启动脚本选 DeepSeek，代码默认模型 `deepseek-v4-flash`，也支持其他已有 provider |
| TTS | Companion 调用 DashScope；按环境配置选择 CosyVoice 或 Qwen 复刻模型，不能仅凭源码确定当前私密环境实际选项 |
| 播放 | Companion 转成 16 kHz PCM16，TWA1 PLAY_START/PLAY_PCM/PLAY_END；ESP32 I2S1 TX GPIO10/11/9，GPIO5 控制放大器 |

现有板端代码已经逐块写入 I2S，不需要等完整回答。采集和播放互斥，没有已实现的 AEC。固件采集上限 30 秒，单次播放最多 320000 字节且墙钟上限 12 秒。新适配器分段续播，单段最多 8 秒/256000 字节，实际分段间隙待实机测试。保留所有固件动作、安全、校准互斥约束。

最小方案：只改 Companion。新模式没有动作工具，不允许 `--allow-motion`，机器人连接后显式 disarm；不识别或执行模型输出中的动作文字。旧 `Start-Tars-Voice.cmd` 不变。

## 官方接口核实与兼容性边界

选用 SC2.0，`dialog.extra.model=2.2.0.0`，`tts.speaker=YOUR_SPEAKER_ID`，人设用 `dialog.character_manifest`。`seed-icl-2.0` 是声音复刻资源标识，不是本端到端模型名。请求明确使用 `pcm_s16le`，避免把默认 Opus 或 32 位 PCM 当成 PCM16。

SC2.0 接入文档说明复刻 2.0 音色可用于 TTS 与实时语音；其旧注册流程要求 `model_type=4`。较新的音色注册页面给出的复刻 2.0 状态示例为 `model_type=5`。不能跨接口把这两个值等同，也不能由 `S_` 前缀或 TTS 试听成功认定端到端兼容。

**此 ID 的账户归属、复刻版本、端到端可用状态尚未核验，既未证明兼容，也未证明不兼容。** 需用户提供不含密钥的控制台音色详情/注册状态，由同一应用的服务权限核实。若不能用于所选模型则停止；没有预设音色回退、重新复刻或自动购买逻辑。

官方来源（2026-11-08 查询）：

- [端到端 WebSocket 接入、模型/音色/事件协议](https://docs.volcengine.com/docs/doubao-voice/end-to-endreal-time-speech-large-model-api-access-document?lang=zh)
- [新版音色注册与状态字段](https://docs.volcengine.com/docs/doubao-voice/tone-training-http?lang=zh)
- [模型列表](https://docs.volcengine.com/docs/doubao-voice/model-list?lang=zh)
- [计费说明](https://docs.volcengine.com/docs/doubao-voice/billing-instructions?lang=zh)

## 实现行为

- `--voice-mode legacy` 为默认，`doubao` 为显式测试选项。
- 16 kHz/16 bit/mono 麦克风按 640 字节、20 ms 节奏上传，持续输入含自然静音，不先攒整句话。
- 云端 24 kHz PCM16 经保留历史与相位的低通重采样变成 16 kHz；收到音频就启动播放，分包上限 512 字节，保持原音量上限 20%。
- ASREnded 或首音频到达时停止采集；整轮播完留 250 ms 尾音间隔再恢复。机器人保留现有 2 秒麦克风启动稳定期。
- 同一云会话支持多轮。采集等待 25 秒、接收等待 30 秒、播放队列等待 5 秒、每轮 90 秒；整次测试默认 180 秒，`--doubao-seconds` 可选 1–600 秒。
- 有界待播队列最多 10 秒云端 PCM，超限停止，禁止静默丢包后继续。超时、断线退出，不自动重试收费请求或重放音频。
- `/cancel`、`/quit`、Ctrl+C 结束当前会话与播放；`/stop` 先发送现有 estop，`/disarm` 先发送现有 disarm，再结束。结束后需重新启动测试。
- 取消会丢弃待播队列、发送 FinishSession 并关闭云连接；板端无立即清队列指令，因此关闭 TWA1 连接，触发现有固件 abort/mute。LAN 故障时仍受原心跳超时保护，不能承诺零延迟停声。
- **不支持语音打断**：播放时麦克风关闭，以避免自回声。控制台取消已实现。若以后要求声学全双工，需要另行验证硬件和 AEC。

## 配置与启动

工作目录：`<repository>\firmware\tars_companion`。
Python：`<repository>\firmware\tars_companion\.venv\Scripts\python.exe`。

`doubao.example.json` 保存模型、音色和可编辑人设，不含密钥。已在上述 G 盘虚拟环境安装 `websockets 15.0.1`、`sounddevice 0.5.6`，安装时 TEMP/TMP 指向 G 盘且禁用 pip 缓存，没有调用模型。重建环境时安装 `pyproject.toml` 中的 `doubao` 可选依赖。

凭据只用当前进程环境变量 `DOUBAO_APP_ID`、`DOUBAO_ACCESS_KEY`。实现采用所选端到端接口明确列出的 App ID/Access Token 鉴权；不要直接把方舟 Key 或新版控制台的 X-Api-Key 当作 Access Token。如果账户仅提供新版 Key，应先核实此端点对应的官方鉴权方式。不要把凭据放进 cmd、源码、Git、日志或聊天。

无需凭据的本地检查：

```powershell
.\.venv\Scripts\python.exe -m tars_companion.robot_runtime --voice-mode doubao --mock
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_doubao.py -q
```

以下命令**尚未执行**，仅在用户明确批准费用、核实音色与凭据并安装依赖后使用：

1. 独立验证，先用电脑麦克风/扬声器：

```powershell
.\Start-Doubao-Standalone.cmd --approve-doubao-cost --speaker-compatible --once
```

2. 独立验证通过且用户确认音色后，关闭旧语音程序，改用机器人麦克风和扬声器：

```powershell
.\Start-Doubao-Robot.cmd --approve-doubao-cost --speaker-compatible --standalone-verified --once
```

3. 去掉 `--once` 测多轮，默认最多 180 秒。

这些开关是操作者的明确确认，不是程序已经核验过账户或实测过音色的证明。未提供确认则在打开网络/设备之前拒绝运行。直接双击新启动脚本不会自动批准付费。

## 费用触发条件：尚未授权，未执行

需要账户具备豆包端到端实时语音服务 `volc.speech.dialog` 权限，以及该应用可使用的复刻音色。在线音频输入、模型生成文本/音频及上下文缓存可能按 token 计费，具体费率、余额和免费额度以账户账单规则为准；不能把已有 TTS 试听权限视为免费端到端权限。

购买/新增音色槽位、开通付费服务、重新注册或训练音色均不在当前执行范围。某些后付费音色首次正式合成会触发槽位转正收费，需核实当前音色计费状态。当前代码不会购买或开通服务，且不调用注册接口；本次没有收费调用。

下一阶段建议审批范围：仅使用现有音色做一次最长 60 秒的独立端到端测试，不购买、不重新复刻；通过后另行确认机器人测试。开始前还需给出可接受费用上限与账户计费状态。

## 验证记录与后续试听

已在上述 G 盘 Python 执行：

| 检查 | 结果 |
| --- | --- |
| `test_doubao.py` | 10 项通过：官方连接帧、JSON/音频/gzip、坏包、重采样跨包一致性、费用/音色/动作门禁、两轮模拟流式响应、停止采集、断线/超时/取消/溢出、分段播放、认证 TCP 回环 |
| `test_wifi_audio.py` | 9 项通过 |
| `test_wireless_runtime.py` | 14 项通过 |
| 旧模式 `robot_runtime --mock --once` | 通过 |
| 新模式 `--voice-mode doubao --mock` | 配置检查通过，不调用云端 |
| 真实服务鉴权、Speaker ID 兼容性 | 未测 |
| 中文、英文、多轮语义、机器人实际出声 | 未测；测试中的中英文文本只验证编码，不验证识别效果 |
| 音色相似度与漂移、分段间隙、自回声、真实延迟 | 未测 |

每轮输出不含密钥和转写内容的 JSON 指标：估计说完到云首音频、服务端 ASREnded 到云首音频、云首音频到本地播放提交、云首音频到机器人首块 PCM ACK。能量阈值估计说完时刻有误差；本地提交与 ACK 都不代表扬声器真实起声。`acoustic_latency_ms` 保持 null，需外部录音对齐用户尾音/机器人首音测量；不要把模拟测试数字当成真实性能。

在线验收顺序：中文自我介绍以外的日常问答；英文问答；中英切换；连续至少 5 轮记忆与追问；长回复验证分段；控制台取消确保旧回答不续播；断网后不自动恢复旧回答。录下同一短文的原 TTS 试听、端到端电脑输出、机器人输出作音量匹配 A/B，记录音色相似度、语速、尾音、长对话漂移与扬声器影响。不能保证端到端音色等同独立 TTS。

## 修改文件与回退

新增：`tars_companion/doubao.py`、`tars_companion/doubao_runtime.py`、`tests/test_doubao.py`、`doubao.example.json`、两个 `Start-Doubao-*.cmd` 和本文档。
修改：`robot_runtime.py` 添加路由与开关；`pyproject.toml` 添加可选依赖。原工程已有的其他未提交更改未回退、未提交。

退出新测试，重新启动原 `Start-Tars-Voice.cmd`，或显式 `--voice-mode legacy`。无需刷回固件、删除 NVS 或更改音色；旧模式仍沿用原动作授权流程。新旧程序不能同时占用同一 LAN 监听端口。
