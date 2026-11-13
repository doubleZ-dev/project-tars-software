# 0.18.3-public：电脑 AI、API 与豆包语音配置

[English](AI_SETUP.en.md) · [返回首页](../README.md)

## 获取与版本

下载 [完整软件包](https://github.com/doubleZ-dev/TASK-TARS-Embodied-Robotics/releases/download/v0.18.3-public/TARS-0.18.3-public.zip)；它替代旧固件和舵机调试两个零散包，包含对应源码、日常语音 Companion 和调试工具，不含拍摄导演台。详细中文操作以包内 `AI_INSTALL.txt`、`DOUBAO_VOICE.txt` 为准。

提供者已报告编译及本地检查通过；0.18.3 完整实机和用户云端账号验收尚未完成。不要将 `--check` 或 `--mock` 的成功视为实机通过。

## 1. 安装与烧录

使用 Windows 10/11 x64、Python 3.13 x64、TARS PCB V1 / ESP32-S3 N16R8。解压到可写目录，运行根目录 `Install.cmd`，它创建 `companion/.venv`、安装固定版本依赖并下载、校验官方 ASR 模型（下载约155 MB，解压约239 MB）。电脑和机器人须在同一可信局域网，机器人使用2.4 GHz WPA2 Wi-Fi。为电脑保留稳定 IPv4 地址。

烧录和配网时断开舵机供电，ESP32 通过 USB 供电。从包根目录执行，先查看并确认自己的端口和 MAC：

```powershell
companion\.venv\Scripts\python.exe -m serial.tools.list_ports -v
companion\.venv\Scripts\python.exe -m esptool --chip esp32s3 --port COMx read_mac
companion\.venv\Scripts\python.exe tools\flash.py --port COMx --device-mac YOUR_DEVICE_MAC
```

最后一条仅显示计划；确认设备和烧录操作后追加 `--apply`。脚本核验固件哈希、芯片、MAC和16 MB Flash并读回验证。地址为 bootloader `0x0`、分区表 `0x8000`、应用 `0x10000`，DIO/80 MHz/16 MB，不整片擦除。

## 2. USB 首次配网与证书

停止其他连接程序，重新确认烧录后的端口，在 `companion` 目录运行：

```powershell
.venv\Scripts\python.exe -X utf8 -m tars_companion.setup_public --port COMx
```

在本机终端填写 Wi-Fi SSID、隐藏输入的密码和电脑IPv4。程序读取机器人身份，生成随机配对 token 和本机专属证书/私钥，通过 USB 写入公开证书与配网信息，私钥留在 `data/tls/`。完成后手动重启机器人。**无需原作者电脑、私钥或重新编译固件。** 防火墙仅允许专用局域网 TCP 8765、8766，不需公网映射。

电脑 IP 改变需重新配对；停止服务后将旧 `wifi-audio.local.json` 和 `data/tls` 移到私有备份目录，再重跑配置。更换电脑不能直接沿用 Windows DPAPI 凭据。不要关闭证书验证。

## 3. API 与豆包声音复刻

1. 在自己的火山引擎豆包语音控制台创建 API Key，启用端到端实时语音服务。
2. 需要克隆声音时，开通声音复刻2.0，使用本人或已获授权的样本训练、试听并取得自己的 Speaker ID。
3. 确认同一 Speaker ID 可用于账号的实时对话与合成接口；只成功合成并不证明实时对话权限有效。
4. 运行 `companion/Configure-Voice.cmd`，在本机安全输入框填写 API Key、Speaker ID 和匹配的 TTS resource。克隆2.0使用 `seed-icl-2.0`；预设音色按其官方资源配置，不盲目沿用。
5. Key 由当前 Windows 用户 DPAPI 加密保存，不写入固件；音色配置保存在 `voice.local.json`。人格可在 `tars-seeduplex.local.json` 的 `instructions` 调整。

`token_budget` 是本程序本地对话额度上限（至少100000），不是云端免费额度。固定提示语合成另按云端实际账单计费。语音动作仅在确认标定与空间后选择启用，未启用仍可聊天。

包内使用 Realtime JSON事件协议、`X-Api-Key`，会话模型 `1.2.6.1`；接口兼容性与账号权限以服务方文档/控制台为准：[实时语音](https://docs.volcengine.com/docs/doubao-voice/end-to-end-real-time-voice-full-duplex-version?lang=zh)、[控制台](https://docs.volcengine.com/docs/doubao-voice/console-guidance?lang=zh)、[声音复刻](https://docs.volcengine.com/docs/doubao-voice/sound-replica-api-v3?lang=zh)。

## 4. 检查与启动

在 `companion` 中按顺序运行：

```powershell
.\Start-Voice.cmd --check
.\Start-Voice.cmd --prepare
.\Start-Voice.cmd
```

`--check` 仅检查本地配置、证书、模型与 Opus，不访问云端或驱动机器人。`--prepare` 使用自己的云端账号合成并缓存提示语，会产生服务调用。正常启动后等待“本地监听中”，先验聊天，再在旁验证一条短动作指令。电脑必须持续运行、不休眠。

前进/转向和数量在电脑离线识别，普通对话发送当前轮语音到豆包。动作执行完成后播放完成提示；**运动与播放时不收音，不支持边动边说或语音喊停**。使用 `Stop-Voice.cmd` 或 Ctrl+C 停止服务；停止服务不是已验证的机械急停功能。断线租约机制仍需结合实际硬件验收。

## 5. USB 调试、编译与排错

先停语音，再运行 `Start-Servo-Debug.cmd` 或 `Start-Gait-Debug.cmd`，一次一个工具。`--mock` 只检查模拟页面。需要自行构建时，在 ESP-IDF 5.5.3 环境进入包内 `firmware-source` 执行 `idf.py build`；首次构建联网下载锁定的托管组件。

| 现象 | 检查 |
| --- | --- |
| 串口占用/配网失败 | 实际端口、烧录后重新枚举、是否其他程序占用 |
| TLS/连不上 | 本机IP、证书与设备身份、专用网络防火墙；重新USB配对 |
| 无法聊天或合成 | 账号API Key、服务开通、Speaker ID及resource权限 |
| 模型缺失 | 重跑Install.cmd，保留下载哈希校验 |
| 动作异常 | 先停止，检查机械零点、接线和结构；不要用动作辨认设备 |

## 隐私与许可

只分享原始发布 ZIP。安装产生的 `*.local.*`、`data/`、`models/`、`.venv/` 不重新上传，尤其不能分享密钥、Wi-Fi密码、token、私钥、对话数据库、录音及运行日志。原创资料遵循 [CC BY-NC-SA 4.0](../LICENSE.txt)，商用需作者另行授权；第三方组件和云服务保留各自许可与条款。
