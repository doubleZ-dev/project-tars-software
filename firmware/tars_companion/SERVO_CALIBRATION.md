# TARS 舵机机械零点校准工具

本工具为本地网页 + Python Companion + 固件专用校准协议，2026-11-07。
只监听 `127.0.0.1`；无 CDN、云服务或 GPT。MG90S 无反馈，**实际角度始终未知**。

## 启动与访问

在 `<repository>\firmware\tars_companion` 运行：

```powershell
python -m tars_companion.servo_calibration
```

访问 `http://127.0.0.1:8766/`，**必须使用终端打印的完整地址，包括 `#随机令牌`**。
令牌每次启动变化，页面加载后从地址栏清除；刷新后请重新打开终端中的完整地址。
默认仅启动服务，不自动打开串口。USB 连接、校准使能及每次发送角度都由用户操作。
已有 Companion 环境即可使用，依赖沿用 pyproject 中的 pyserial；无需额外 Web 框架。
安装包也提供 `tars-servo-calibrate` 命令及页面静态资源。

离线体验同一工具，完全不打开串口：

```powershell
python -m tars_companion.servo_calibration --mock
```

Mock 页面及导出文件均标记为虚拟设备，不可作为实物执行证据。
退出用 Ctrl+C；正常退出尽力关闭输出，进程意外终止由固件独立租约兜底。
本次开发未连接、未烧录、未操作真实舵机。设备必须先经另外授权安装包含新接口的固件；旧固件会拒绝校准命令，页面不会伪造成功。

## 操作步骤（需要用户实物配合）

1. 保持舵臂、连杆已拆开，支撑机器人，确认四个插头编号、方向及公共地。GPIO 固定为 Servo1→38、Servo2→39、Servo3→40、Servo4→41。
2. 先关闭串口监视器、旧 Companion、烧录工具等。打开本机页面，获取网页控制权，选择具有唯一序列号的原生 USB Serial/JTAG 设备，点击连接；确认显示 USB 握手成功。
3. 勾选所选卡片的安全确认，再点击“选择该舵机并确认校准使能”。**该按钮会只使能此轴，并发送参考中位 0° / 1500μs**；其他三路强制低电平。不调用普通 safety.arm。
4. 观察此轴是否可自由转动，遇异响、碰撞或供电异常立即点“关闭全部舵机输出”或“软件急停”，必要时使用实体电源开关。关闭 PWM 不是断电。
5. 编辑输入框或滑块，或用 ±1° 按钮微调草稿。**只有“发送目标角度”和“回参考中位”会下发目标**。初始固定保守范围 −20°～+20°，不会扩展至全行程。
6. 人工判断机构机械零点后，勾选“我已人工确认当前目标对应机构机械零点”，点击标记。记录取自最后一次固件完成响应，不能把尚未发送的草稿记成零点。每次修改目标后重新确认。
7. 切换另一轴时先关闭前一轴，再只使能新轴。完成后导出 JSON，并关闭全部输出。重新连接、清除急停均不恢复输出或旧角度。
8. 在输出关闭状态下按装配流程安装舵臂/连杆，人工复核记录、方向及机械限位。需要输出保持来辅助装配时，必须另行确认操作条件；不要把导出偏移直接写入步态。供电和实际动作验收仍由用户完成。

按钮、复选框、输入框、滑块均使用原生键盘可操作控件（Tab、空格、Enter、方向键）。页面保持前台并有焦点才发送校准续租；切到其他窗口/标签页、最小化或关闭会撤销控制权，需要重新获取并重新确认使能。导出 JSON 可能打开系统保存窗口，此时也会安全停输出。

## 三种角度与 PWM

- **舵机参考中位**：驱动物理坐标 `0°`，参考脉宽 `1500μs`；不是常见标称 0～180° 坐标，也不是已经对齐机构的保证。
- **相对参考中位的校准角**：在当前驱动坐标中直接给定 −20～+20 的整数。保留原硬限位 −90～+90；实际更窄的机构安全范围必须人工验证。
- **用户确认的机构机械零点**：用户判断某个已下发目标使机构对齐时记录的偏移，不是编码器测量值。

核对并复用现有驱动映射：`pulse_us = 500 + (angle_deg + 90) * 2000 // 180`，50Hz。
因此 ±20° 对应 1277～1722μs，1°步长因整数截断对应约11～12μs。
校准不经过 gait 的 `logical_to_physical`，不叠加历史 +5/−10/0/+10，也不应用历史方向反转。
正式步态映射保持原样；结果仅导出供人工确认，**不写 NVS、不改正式步态零点**。

## 安全与会话设计

- 固件默认四路强制低。校准与普通 Arm/步态/音频自检互斥。音频会话准入与校准使能共享互斥锁，避免两个任务同时通过检查。已有 Wi-Fi 音频会话存在时校准会被拒绝；先停止音频 Companion 并等待音频会话结束。
- 每次校准使用新的 calibration_id，进入时必须 confirm=true。一轴使能期间拒绝再次 begin；网页切轴先 end。过期、断连、取消、急停均销毁租约；旧租约不可复活。
- 页面前台每250ms请求一次；Python网页租约900ms。只有该网页的有效 ping 能触发一次 calibration.renew，Python无后台角度或校准续租循环。
- 固件校准租约1200ms，由50ms看门狗巡检。普通 session.keepalive 及其他 JSON 消息只续原控制租约，**不能续校准租约**。停止页面续租后约1.2s另加调度时间强制关闭；不能称为硬件急停或精确实时保证。
- MCPWM 先强制关闭所有路，再设置选中轴比较器，等待一个完整20ms周期加载，最后仅释放选中路；校准状态与关闭/急停共享锁。
- 原生 USB 的 boot ID、session ID、command ID、ACK、完成响应、32项有界去重缓存继续使用。重复相同命令只重放响应，冲突 ID 被拒绝；重试不恢复新会话中的旧动作。
- UI将“草稿”“已下发目标”和“实际角度未知”分开。设备拒绝、ACK/完成响应丢失、USB会话变化会报错；关闭请求未确认时显示未知，不把前端变化当成硬件成功。
- 只接受精确127.0.0.1 Host和同源Origin、JSON、随机请求令牌；限制请求大小，无CORS。单网页owner令牌，无自动接管。Windows串口沿用pyserial独占打开，多个程序抢同一COM会失败；不同服务实例也无法共享此端口。此为本机网页请求防护，不用于防御已控制电脑的恶意本地进程。
- 记录保存在当前Python进程内，可跨网页重新获取控制权导出；结束进程前请导出，否则记录不持久化。导出包含mock/evidence、轴/GPIO、参考角/脉宽、校准角/脉宽、用户确认零点、UTC时间、boot/command ID、actual_angle_deg=null。

## 修改文件

Companion：`tars_companion/servo_calibration.py`、`tars_companion/servo_web/{index.html,app.js,style.css}`、`simulator.py`、`pyproject.toml`、`tests/test_servo_calibration.py`、`tests/servo_browser.cjs`、本说明及 `servo-zero.example.json`。

Firmware：`components/tars_motion/{tars_motion.c,tars_servo_mcpwm.c,include/tars_motion.h}`、`components/tars_protocol/tars_protocol.c`、`components/tars_wifi_audio/tars_wifi_audio.c`（只加校准互斥门控）、`tests/test_servo_calibration.py`、协议附录。
未修改 PCB GPIO、实体双屏 UI/字体、步态帧/历史偏移、人格、云服务、Wi-Fi 音频协议/配置。保留既有 `docs/STAGE2_STATUS.md` 编辑。

## 验证与证据

- 16项校准/HTTP异步测试通过：确认、保守范围、单轴Mock门控、独立超时、旧租约拒绝、原心跳不能续租、取消/急停、第二页面拒绝、失活、重连/重启不恢复、重复命令不重复执行、错误ACK不记成功、关闭响应丢失仍显示未知、JSON记录、Host/Origin/token。
- 13项本地真实 Chromium 页面检查通过（服务与设备为Mock）：草稿不驱动、发送/参考中位、手工记录/JSON、全局关闭、失焦、重连。截图 `evidence/servo-page-mock.png`，检查清单 `evidence/servo-browser.json`。
- 117项相关Companion回归通过，包含上面16项，覆盖USB、传输、协议、Brain边界及音频框架；`evidence/servo-regression.log`。没有调用付费/真实LLM或连接硬件。
- 固件31项静态/算术模型检查通过（7项新增校准约束），`../tars_firmware/evidence/servo-contracts.log`。**这些不是固件运行测试或PWM实测**。
- 广泛旧测试探索未作为全量通过：历史部署测试缺失发布目录/manifest，旧WAV测试patch已不存在的SerialTransport，均未改动。最终采用明确的相关回归集合；不宣称整个仓库测试全绿。
- 固件最终构建结果及SHA见 `../tars_firmware/evidence/servo-image.json`、`servo-build-final.log`；仅构建，不烧录。新增接口的候选构建仍沿用Stage4版本字符串，以该SHA及 hello 中 servo_calibration_v1 区分，不可拿原Stage4镜像替代。

可重跑：Companion目录 `python -m unittest discover -s tests -p test_servo_calibration.py -v`；固件目录 `python -m unittest discover -s tests -v`。页面脚本为本机Windows Chromium CDP检查，`node tests/servo_browser.cjs`，仅启动Mock和独立无头浏览器；不操作现有浏览器或串口。

实物剩余验收：经授权安装候选固件；逐路确认GPIO/PWM、启动无脉冲、其他三路保持低；实际USB拔出、杀进程、页面关闭、冻结与丢心跳、急停；示波器确认单路脉宽/50Hz及超时；人工机械零点/供电/抖动验收。没有传感器，任何阶段都不能测得舵机真实角度。

## 当前装配位置核对（2026-11-07）

用户将“中间偏左”的舵机插入 Servo1/U2/GPIO38 后，对原步态编号提出疑问。
这仅描述本次插接，不能据此重定义历史算法的1号。已撤销刚加入的“Servo1=中间偏左”位置标签；未改GPIO、步态或固件，未发送使能/角度指令。

历史依据：`../../animation_20261106/left_turn_independent/left_turn_manifest.json` 的 servo_mapping（program_confirmed_by_user=true）：1_GPIO9=left leg，4_GPIO12=right leg，2_3=central mirrored pair。
因此历史1号为左侧外腿、4号为右侧外腿；2/3为中间镜像升降两轴。现有记录未明确2/3各自的左右，暂不猜测。
PCB V1沿用编号，对应新GPIO38/39/40/41；应核对插接对应关系后再使能，不能通过重排算法来适应临时插接。

### 最新接线确认

用户确认保留原编号与算法，自行调整舵机插接顺序；目前已将**左侧外腿接到Servo1/U2/GPIO38**。
网页及导出位置标签沿用原1号=左侧外腿、4号=右侧外腿；2/3的左右实体位置仍待确认。不重排编号、不改步态或方向、不重新烧录。
校准固件已在用户授权、四只舵机拔下时烧录并校验成功，物理RESET后真实USB握手及未使能状态查询通过；此前“仅离线开发”的描述保留为开发阶段记录，最新烧录证据见固件evidence/servo-install-status.json及Companion evidence/servo-real-nonmotion.json。尚未执行舵机使能或角度命令。

### 用户声明的装配机械零点（2026-11-07）

用户在最新截图明确声明Servo1～4的机械零点依次为 **+10°、0°、−10°、+10°**（相对1500μs参考中位）。已单独记录于`servo-zero.user-confirmed-2026-11-07.json`，来源为用户对截图的确认；这不是实际角度测量，也不是带命令ACK的网页导出记录。
固件正式步态零点仍为+5/−10/0/+10，方向仍为+1/−1/+1/−1；未修改、未烧录、未下发步态。按用户新零点计算的步态仅供评审，不能将其称为当前固件已采用的值。

### 机械零点修订：Servo4 = +2°

用户随后提供Servo4截图（已下发目标2°/1522μs，固件完成响应）并明确要求更换该轴机械零点。
最新用户确认零点为 **[+10°, 0°, −10°, +2°]**，取代上文第四轴+10°。其他轴、方向系数和前进逻辑关键帧保持不变。
单独JSON记录已更新并保留修订来源；只重算前进目标，未覆盖正式固件零点、未烧录、未下发步态。

### 正式集成更新（取代上文“未覆盖正式固件零点”的当前状态）

用户已明确要求将确认的步态更新到总软件。正式源码 `../tars_firmware/components/tars_motion/tars_motion.c` 的共用机械零点现为 **[10,0,-10,2]**，方向仍为[1,-1,1,-1]。不改变三套逻辑关键帧；校准模式参考0仍为1500μs，不叠加新旧步态零点。
此修改影响前进/左转/右转及普通Arm回零。当前阶段仅更新源码与构建，不烧录，实际板子仍运行先前安装的旧偏移镜像。总软件请读取 `../tars_firmware/docs/GAIT_CALIBRATION_HANDOFF_2026-11-07.md` 继续集成。
