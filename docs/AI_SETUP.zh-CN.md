# 电脑端 AI 大脑、API 与豆包语音克隆配置

[English](AI_SETUP.en.md) · [返回项目首页](../README.md)

## 先区分三个部分

| 部分 | 运行位置 | 是否需要云端账号 |
| --- | --- | --- |
| 板端固件 | ESP32-S3 | USB 调试不需要云端账号 |
| 舵机调试工具 | 电脑上的本机网页与 Python 服务，通过 USB 连接机器人 | 不需要 |
| 语音 Companion | 电脑常驻服务，与机器人通过局域网通信 | 云端对话、合成及克隆音色需要自己的服务账号 |

只下载固件和舵机调试包不会自动装好语音 Companion。作者正在整理最终公开语音交付；本页先说明配置原则与当前仓库入口。历史仓库源码不是已确认与 `0.18.2-final65` 二进制完全对应的最终源码。

## 1. 准备电脑与依赖

电脑和机器人需要可互通的局域网，机器人接入 2.4 GHz Wi-Fi。云端对话还需要互联网。电脑的局域网 IP 建议固定或在路由器中保留，避免重启后地址变化导致连接或证书不匹配。

当前仓库可从 `firmware/tars_companion` 查看电脑端代码。安装 Python 后，在该目录创建独立环境：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[daily]"
```

USB 调试包要求 Python 3.11 或更新版本；完整语音部署以最终包的依赖要求为准。唤醒/识别模型不放进本仓库，应按所用服务版本下载对应模型；不要混用历史唤醒模型与新版识别模型。旧版持续语音说明见 [TARS_VOICE_SERVICE.md](../firmware/tars_companion/TARS_VOICE_SERVICE.md)，新包交付后以新包说明为准。

## 2. 填写自己的局域网配对信息

复制 `wifi-audio.example.json` 为 `wifi-audio.local.json`，按模板填写：

```json
{
  "ssid": "YOUR_2_4_GHZ_WIFI",
  "password": "YOUR_WIFI_PASSWORD",
  "host": "192.168.1.100",
  "port": 8765,
  "token": "REPLACE_WITH_YOUR_RANDOM_PAIRING_TOKEN"
}
```

这里的 IP 只是示例，必须替换为运行 Companion 的电脑地址。配对 token 使用自己的随机值：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

首次通过 USB 写入配网数据时使用自己设备的标识：

```powershell
.\.venv\Scripts\python.exe -m tars_companion.wifi_config --config wifi-audio.local.json --usb-serial YOUR_USB_SERIAL --apply
```

写入后重启机器人，并允许 Companion 在 Windows 专用网络上通信。需要 `TARS_DEVICE_ID` 的入口应填写自己的机器人 MAC 地址，不能照搬作者设备信息。

## 3. 为自己的电脑配对 TLS 证书

当前最终二进制内置原机 Companion 的公钥证书。**全新电脑仅更换 Wi-Fi、token 和 API Key，并不能保证与这个现成二进制完成语音连接。** 原机的私钥不公开，也不应复制给复刻者。

历史源码已有 `firmware/prepare_xiaozhi_tls.py`：读取自己的 `wifi-audio.local.json`，在电脑 `data/tls/` 中生成私钥与证书，只把公钥证书放入固件源码，然后需要编译、烧录相匹配的应用。

从仓库根目录操作历史源码的路径为：

```powershell
firmware\tars_companion\.venv\Scripts\python.exe firmware\prepare_xiaozhi_tls.py
```

再在 ESP-IDF 5.5.3 终端进入 `firmware/tars_director_firmware` 构建匹配该历史源码的固件。**这个路径是现有源码的证书机制说明，不代表已交付新版独立语音固件。** 最终独立语音包确定后，将同步其确切构建/配对入口。

保留自己的匹配证书和私钥。若电脑地址改变、证书丢失或重新生成，应同时处理固件信任关系；不要通过关闭 TLS 校验解决配对问题。

## 4. 获取豆包 API Key

进入豆包语音/火山引擎控制台，选择自己的目标项目，在“API 管理”创建 API Key，并在“开通管理”启用需要的语音服务。API Key、服务和音色必须属于有权限的项目。[官方控制台指引](https://docs.volcengine.com/docs/doubao-voice/console-guidance?lang=zh)

本项目语音入口读取 `DOUBAO_API_KEY`。现有 `Start-Tars.ps1 -Setup` 可以通过隐藏输入保存为当前 Windows 账户加密的本地凭据。也可以只在当前 PowerShell 会话设置，避免把真实 Key 写进示例文件：

```powershell
$tarsSecret = Read-Host '豆包 API Key' -AsSecureString
$env:DOUBAO_API_KEY = [System.Net.NetworkCredential]::new('', $tarsSecret).Password
$tarsSecret.Dispose()
```

从同一个终端启动服务。关闭服务后可执行 `Remove-Item Env:DOUBAO_API_KEY` 清理当前会话变量。不要把实际 Key 放进 README、截图、Issue 或公开 ZIP。若使用其他模型服务，应按对应入口配置其单独凭据；豆包语音 Key 不等于其他平台的模型 Key。

## 5. 克隆自己的音色

1. 在豆包语音控制台选择目标项目，启用适用的声音复刻服务，并获取自己的复刻音色槽位/ID。
2. 使用自己的声音或已获授权的素材，按控制台或官方接口要求提供干净样本及必要文本。
3. 按[官方音色注册接口](https://docs.volcengine.com/docs/doubao-voice/tone-training-http?lang=zh)完成训练，检查返回结果并试听。当前注册接口为 `POST /api/v3/tts/voice_clone`，新版鉴权使用 `X-Api-Key`；具体字段、格式和限制以官方文档为准。
4. 保留自己的 Speaker ID。不要照抄历史示例里的作者音色 ID，它不代表自己的账号拥有该音色权限。
5. 在对应语音接口单独验证音色能正常合成，再接入机器人。TTS 音色训练成功不等于任意实时对话接口都支持同一音色或参数。

声音复刻、语音合成和实时对话的资源/计费可能不同，请按控制台确认。使用声音复刻 2.0 的 TTS 接口时资源标识为 `seed-icl-2.0`，具体请求以[官方 TTS 文档](https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http?lang=zh)为准，不要把这个标识直接套进所有实时对话请求。

## 6. 把 Speaker ID 与人格配置交给 Companion

当前仓库的 `seeduplex.py` 从环境变量读取音色：

```powershell
$env:TARS_TTS_SPEAKER = 'YOUR_SPEAKER_ID'
```

复制 `tars-seeduplex.example.json` 为 `tars-seeduplex.json`，在 `instructions` 中设置人格与回复偏好。不要把私人历史对话直接写入公开示例。

在已完成配对、模型准备与证书配置的环境中，现有持续语音入口可运行：

```powershell
.\Start-Tars.ps1 -Setup -ChatOnly
```

先验证聊天与播放，再按该程序的确认流程启用动作。旧入口会询问控制台剩余额度，这只是该版本的本地额度策略，不代表账户自动获得免费额度。最终独立日常语音包的启动方式尚待作者补充；本次不发布拍摄导演台作为配置入口。

## 7. 检查顺序与排错

1. 先验证 USB 调试：显示和单舵机校准正常。
2. 再验证局域网、配对 token、设备 ID 和证书信任关系。
3. 单独验证云端 API 鉴权与音色权限。
4. 启动 Companion，测试收音和回复播放。
5. 最后开启语音动作，逐项测试；电脑服务保持运行。

| 问题 | 优先排查 |
| --- | --- |
| 鉴权失败 | Key 是否属于正确项目、相应服务是否开通 |
| 音色无权限 | Speaker ID、账号项目、所选接口是否兼容 |
| TLS / 无法连上机器人语音通道 | 电脑地址、公钥证书与私钥是否匹配、固件是否信任该证书 |
| 串口忙 | 是否同时运行校准、步态或其他占用 USB 的程序 |
| 服务启动后没有声音 | 所需模型、音频模块接线、服务日志中的错误码；不要公开原始私人日志 |

## 隐私与许可

自己的 Wi-Fi 配置、token、API Key、TLS 私钥、凭据 XML、对话数据库、录音和运行日志只保留在本机。分享排错信息时提供经过脱敏的错误码与版本。

项目原创资料采用 [CC BY-NC-SA 4.0](../LICENSE.txt)，未经授权不得用于商业用途。云端服务、音色、模型及第三方软件另遵循各自使用条款和许可证。
