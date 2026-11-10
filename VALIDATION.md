# 本次软件导出验证

日期：2026-11-10。

## 文件检查

- 导出最新 `0.18.0-director` 固件、当前 Companion、网页资源、测试和示例配置。
- 排除机械设计、动画、音频素材、历史 Flash 备份、编译输出、虚拟环境、凭据、TLS 密钥/本机证书、Wi-Fi 配置和个人文字记忆。
- 对导出文本执行常见密钥格式检查，并与本机配置中的实际口令/令牌作字符串比对，未发现匹配；这不是完整安全审计。
- LVGL 和 ST7735 驱动的源码与本地上游校验清单一致（LVGL 缺少部分上游文档）；改由固定版本依赖清单获取，避免提交重复依赖。
- TLS 生成脚本的目标目录改为本次保留的 `tars_director_firmware`。

## 离线测试

在导出目录使用开发机现有 Python 虚拟环境运行：

```text
python -m unittest discover -s tests
Ran 285 tests in 43.131s
FAILED (failures=3, errors=15)
```

即本次运行 267 项通过，3 项断言失败、15 项错误。

失败/错误的具体范围：

- 3 项断言失败：`test_brain` 的越界步数预期，以及 `test_gait_debug` 的两项 drop-ramp 保护预期，与当前实现不一致，需要单独排查。
- 1 项错误：旧 `test_audio_calibration` 尝试 mock 当前模块已经没有的 `SerialTransport` 属性。
- 5 项错误：`test_deployment` 依赖未分发的旧发布 manifest 和固件二进制。
- 1 项错误：本机测试环境缺少 `httpx`，无法导入 `test_openai`。
- 1 项错误：`test_seeduplex` 依赖已排除的个人配置 `tars-seeduplex.json`，应改用测试专用配置。
- 1 项错误：`test_voice_gait_release` 仍引用旧 `tars_firmware` 路径。
- 6 项错误：`test_xiaozhi_audio` 依赖本机 TLS 证书；这些证书和密钥有意未分发，应在测试中临时生成。

本次工作范围为整理与上传软件，没有为使测试变绿而改写现有业务逻辑或删除失败测试。没有重新编译固件、烧录设备、运行真实舵机或验收云端语音。不能把上述离线测试结果当作硬件验收结果。
