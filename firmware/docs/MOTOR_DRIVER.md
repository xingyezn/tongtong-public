# 双直流电机驱动（TB6612FNG）

`bread-compact-wifi` 板型已经接入一个适用于 TB6612FNG（以及引脚兼容双 H 桥）的开环双直流电机控制器。固件上电默认使 `STBY` 为低电平，电机不会自行转动。

## 接线

| TB6612FNG 引脚 | ESP32-S3 引脚 | 说明 |
| --- | --- | --- |
| AIN1 / AIN2 | GPIO 8 / GPIO 9 | 左电机方向 |
| PWMA | GPIO 10 | 左电机 PWM（20 kHz） |
| BIN1 / BIN2 | GPIO 11 / GPIO 12 | 右电机方向 |
| PWMB | GPIO 13 | 右电机 PWM（20 kHz） |
| STBY | GPIO 14 | 驱动使能；空闲时为低 |
| VCC | ESP32 的 3.3 V | 逻辑电源 |
| VM | 电机额定电源 | 电机电源，按电机与驱动板规格提供 |
| GND | ESP32 GND 与电机电源 GND | 必须共地 |
| A01/A02、B01/B02 | 左、右直流电机 | 如方向相反可交换对应电机两根线 |

> 不要从 ESP32 开发板的 3.3 V、5 V 或 USB 口给电机供电。电机电源应有足够的电流余量，并与 ESP32 共地。

## MCP 控制接口

设备向后端公布以下工具：

- `self.chassis.go_forward`、`go_back`、`turn_left`、`turn_right`：参数为 `speed`（0–100，默认 60）和 `duration_ms`（1–10000，默认 1000）。
- `self.chassis.drive`：分别指定 `left_speed`、`right_speed`（-100–100）与 `duration_ms`，用于微调或弧线行驶。
- `self.chassis.stop`：立即停止两路电机并关闭驱动待机使能。
- `self.chassis.get_state`：读取控制器当前状态。

所有运动命令都是**限时动作**：时间结束后会将 PWM 置零、方向引脚置低，并拉低 `STBY`。因此语音链路、网络或模型异常时不会让电机无限持续运行；`stop` 仍可作为紧急停止指令。

## 首次测试

1. 先断开电机电源，刷入并启动固件，确认设备能正常连网。
2. 将车架垫起，使车轮离地；接入电机供电。
3. 依次以低速、短时测试 `go_forward`、`go_back`、`turn_left`、`turn_right`。
4. 若“前进”方向错误，先交换该电机的两根电机线；也可在 `main/boards/bread-compact-wifi/config.h` 中把相应 `MOTOR_*_REVERSED` 改为 `true` 后重新编译。

该版本没有编码器、避障或闭环速度控制；不要在无人看管、靠近台阶或障碍物的环境中运行。
