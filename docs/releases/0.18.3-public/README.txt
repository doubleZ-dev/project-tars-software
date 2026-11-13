TARS 0.18.3-public 完整安装包

给自己的 AI 安装助手发送 AI_INSTALL.txt，按步骤协助安装。

firmware/          可烧录 ESP32-S3 固件及地址/校验清单
firmware-source/   对应 ESP-IDF 源码，含所需 LVGL 核心，不含第三方演示资源
companion/         日常语音服务、舵机调试页面、配置模板和启动器
tools/             固件校验烧录与官方模型下载工具
Install.cmd        Windows 电脑依赖安装入口
DOUBAO_VOICE.txt   豆包账号、音色和声音复刻配置
AI_INSTALL.txt     完整安装步骤和验收方法

不包含拍视频用的 AI 导演台、任何现成个人账号或密钥。
电脑 Companion 须保持运行。首次联网安装会下载 Python 依赖和本地识别模型。
证书和配对凭据由每个使用者在自己的电脑生成，USB 首次写入，无须作者原机。
系统支持 Windows x64 / Python 3.13，固件对应 TARS PCB V1 / ESP32-S3 N16R8。

源码编译：在 ESP-IDF 5.5.3 环境进入 firmware-source，运行 idf.py build。
两个 Espressif 托管组件由 main/idf_component.yml 与 dependencies.lock 声明，首次编译需联网下载。
第三方许可证随组件保留；项目自有代码的开源许可证由作者发布时指定。
