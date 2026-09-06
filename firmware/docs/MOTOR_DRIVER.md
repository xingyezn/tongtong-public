# 双直流电机驱动（DRV8833）

`bread-compact-wifi` 板型已经接入配件资料中选用的 DRV8833 开环双直流电机控制器。固件上电默认使 `STBY` 为低电平，电机不会自行转动。

## 接线

| DRV8833 引脚 | ESP32-S3 引脚 | 说明 |
| --- | --- | --- |
| AIN1 / AIN2 | GPIO 8 / GPIO 9 | 左电机方向 / PWM（20 kHz） |
| BIN1 / BIN2 | GPIO 10 / GPIO 11 | 右电机方向 / PWM（20 kHz） |
| STBY | GPIO 12 | 驱动使能；空闲时为低 |
| VM | 2.7–10.8 V 电机电源 | 按电机与驱动板规格提供 |
| GND | ESP32 GND 与电机电源 GND | 必须共地 |
| AO1/AO2、BO1/BO2 | 左、右直流电机 | 如方向相反可交换对应电机两根线 |

> 不要从 ESP32 开发板的 3.3 V、5 V 或 USB 口给电机供电。电机电源应有足够的电流余量，并与 ESP32 共地。ESP32 的 GPIO 仅连接 AIN/BIN/STBY 控制输入，使用前应确认模块对 3.3 V 高电平的兼容性。

## MCP 控制接口

设备向后端公布以下工具：

- `self.chassis.go_forward`、`go_back`、`turn_left`、`turn_right`、`spin`：参数为 `speed`（0–100，默认 60）和 `duration_ms`（1–10000，默认 1000）。其中 `spin` 让左右轮反向旋转，执行原地转圈。
- `self.chassis.drive`：分别指定 `left_speed`、`right_speed`（-100–100）与 `duration_ms`，用于微调或弧线行驶。
- `self.chassis.stop`：立即停止两路电机并关闭驱动待机使能。
- `self.chassis.get_state`：读取控制器当前状态。

所有运动命令都是**限时动作**：指令立即返回，由独立定时器在时间结束后将 PWM 置零并拉低 `STBY`。因此语音链路、网络或模型异常时不会让电机无限持续运行；运动期间 `stop` 仍可随时作为紧急停止指令。

## 首次测试

1. 先断开电机电源，刷入并启动固件，确认设备能正常连网。
2. 将车架垫起，使车轮离地；接入电机供电。
3. 依次以低速、短时测试 `go_forward`、`go_back`、`turn_left`、`turn_right`、`spin`。
4. 若“前进”方向错误，先交换该电机的两根电机线；也可在 `main/boards/bread-compact-wifi/config.h` 中把相应 `MOTOR_*_REVERSED` 改为 `true` 后重新编译。

该版本没有编码器、避障或闭环速度控制；不要在无人看管、靠近台阶或障碍物的环境中运行。
