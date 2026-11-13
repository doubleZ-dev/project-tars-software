# 第三方组件与资源

项目整体设计与集成由作者独立完成，以下基础软件及资源来自第三方，不属于原创基础库。

| 组件 | 使用方式 |
| --- | --- |
| ESP-IDF / FreeRTOS | 用户安装 ESP-IDF 5.5.3 工具链；遵循其上游许可 |
| LVGL 9.2.0 | 组件管理器下载；MIT 许可副本位于 `licenses/LVGL.txt` |
| Waveshare esp_lcd_st7735 1.0.1 | 组件管理器下载；上游组件声明 MIT |
| Espressif esp_websocket_client 1.6.1 / esp_audio_codec 2.5.0 | 组件管理器下载；遵循各自上游许可 |
| libopus Windows DLL | 保留于 Companion 的 `native/`；同目录包含 Opus、discord.py 许可及来源/哈希记录 |
| Python 依赖 | 由 `pyproject.toml` 声明并通过 pip 安装，遵循各自许可 |

`tars_font_cjk_12.c` 的生成注释记录其来源为 Windows SimHei 字体。公开源码并不自动解决字体派生资源的再分发授权；后续统一开源发布前应确认该字体资源授权或改用许可明确的开源字体。历史图像头文件与角色名称也不因项目代码公开而获得额外授权。

云端模型、服务及音色不随本仓库分发，需要使用者自行配置有权限的服务资源。作者原创代码采用 CC BY-NC-SA 4.0，商业使用需另行联系作者取得授权；第三方许可保持不变。
