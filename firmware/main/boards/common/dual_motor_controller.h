#ifndef __DUAL_MOTOR_CONTROLLER_H__
#define __DUAL_MOTOR_CONTROLLER_H__

#include <string>
#include <mutex>

#include <driver/gpio.h>
#include <driver/ledc.h>
#include <esp_err.h>
#include <esp_timer.h>

#include "mcp_server.h"

// A safe, open-loop controller for a DRV8833-compatible dual H-bridge.
// PWM is applied to the direction inputs because the DRV8833 has no separate
// PWM pins. At most one input of a motor is driven at a time.
// Every MCP motion command is time-bounded and releases STBY when finished.
class DualMotorController {
private:
    static constexpr int kPwmFrequencyHz = 20000;
    static constexpr int kPwmResolutionBits = 10;
    static constexpr int kPwmMaxDuty = (1 << kPwmResolutionBits) - 1;
    static constexpr int kDefaultSpeed = 85;
    static constexpr int kDefaultDurationMs = 600;
    static constexpr int kMaxDurationMs = 10000;

    gpio_num_t left_in1_gpio_;
    gpio_num_t left_in2_gpio_;
    gpio_num_t right_in1_gpio_;
    gpio_num_t right_in2_gpio_;
    gpio_num_t standby_gpio_;
    bool left_reversed_;
    bool right_reversed_;
    int last_left_speed_ = 0;
    int last_right_speed_ = 0;
    bool direct_mode_ = false;
    esp_timer_handle_t stop_timer_ = nullptr;
    std::mutex mutex_;

    static int Clamp(int value, int minimum, int maximum) {
        if (value < minimum) {
            return minimum;
        }
        if (value > maximum) {
            return maximum;
        }
        return value;
    }

    void ConfigureOutput(gpio_num_t gpio) {
        gpio_config_t config = {
            .pin_bit_mask = (1ULL << gpio),
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_ERROR_CHECK(gpio_config(&config));
        ESP_ERROR_CHECK(gpio_set_level(gpio, 0));
    }

    void SetPwmDuty(ledc_channel_t channel, int speed) {
        int duty = Clamp(speed, 0, 100) * kPwmMaxDuty / 100;
        ESP_ERROR_CHECK(ledc_set_duty(LEDC_LOW_SPEED_MODE, channel, duty));
        ESP_ERROR_CHECK(ledc_update_duty(LEDC_LOW_SPEED_MODE, channel));
    }

    void ApplyMotor(ledc_channel_t forward_channel, ledc_channel_t reverse_channel,
                    int speed, bool reversed) {
        speed = Clamp(speed, -100, 100);
        if (reversed) {
            speed = -speed;
        }

        // Clear both inputs before enabling one direction. 00 is the DRV8833
        // coast state; this prevents a direction change from briefly braking.
        SetPwmDuty(forward_channel, 0);
        SetPwmDuty(reverse_channel, 0);
        if (speed > 0) {
            SetPwmDuty(forward_channel, speed);
        } else if (speed < 0) {
            SetPwmDuty(reverse_channel, -speed);
        }
    }

    void StopPwmOutputs() {
        ESP_ERROR_CHECK(ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, 0));
        ESP_ERROR_CHECK(ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_2, 0));
        ESP_ERROR_CHECK(ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_3, 0));
        ESP_ERROR_CHECK(ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_4, 0));
    }

    void ConfigureDirectOutputs() {
        gpio_reset_pin(left_in1_gpio_);
        gpio_reset_pin(left_in2_gpio_);
        gpio_reset_pin(right_in1_gpio_);
        gpio_reset_pin(right_in2_gpio_);
        ConfigureOutput(left_in1_gpio_);
        ConfigureOutput(left_in2_gpio_);
        ConfigureOutput(right_in1_gpio_);
        ConfigureOutput(right_in2_gpio_);
    }

    void ApplyDirectMotor(gpio_num_t in1_gpio, gpio_num_t in2_gpio, int direction, bool reversed) {
        if (reversed) {
            direction = -direction;
        }
        if (direction > 0) {
            ESP_ERROR_CHECK(gpio_set_level(in1_gpio, 1));
            ESP_ERROR_CHECK(gpio_set_level(in2_gpio, 0));
        } else if (direction < 0) {
            ESP_ERROR_CHECK(gpio_set_level(in1_gpio, 0));
            ESP_ERROR_CHECK(gpio_set_level(in2_gpio, 1));
        } else {
            ESP_ERROR_CHECK(gpio_set_level(in1_gpio, 0));
            ESP_ERROR_CHECK(gpio_set_level(in2_gpio, 0));
        }
    }

    void DriveLocked(int left_speed, int right_speed) {
        if (direct_mode_) {
            StopLocked();
        }
        left_speed = Clamp(left_speed, -100, 100);
        right_speed = Clamp(right_speed, -100, 100);
        if (left_speed == 0 && right_speed == 0) {
            StopLocked();
            return;
        }

        ESP_ERROR_CHECK(gpio_set_level(standby_gpio_, 1));
        ApplyMotor(LEDC_CHANNEL_1, LEDC_CHANNEL_2, left_speed, left_reversed_);
        ApplyMotor(LEDC_CHANNEL_3, LEDC_CHANNEL_4, right_speed, right_reversed_);
        last_left_speed_ = left_speed;
        last_right_speed_ = right_speed;
    }

    void StopLocked() {
        if (direct_mode_) {
            ESP_ERROR_CHECK(gpio_set_level(left_in1_gpio_, 0));
            ESP_ERROR_CHECK(gpio_set_level(left_in2_gpio_, 0));
            ESP_ERROR_CHECK(gpio_set_level(right_in1_gpio_, 0));
            ESP_ERROR_CHECK(gpio_set_level(right_in2_gpio_, 0));
        } else {
            SetPwmDuty(LEDC_CHANNEL_1, 0);
            SetPwmDuty(LEDC_CHANNEL_2, 0);
            SetPwmDuty(LEDC_CHANNEL_3, 0);
            SetPwmDuty(LEDC_CHANNEL_4, 0);
        }
        ESP_ERROR_CHECK(gpio_set_level(standby_gpio_, 0));
        last_left_speed_ = 0;
        last_right_speed_ = 0;
        if (direct_mode_) {
            direct_mode_ = false;
            InitializePwm();
        }
    }

    void StopTimer() {
        if (esp_timer_is_active(stop_timer_)) {
            ESP_ERROR_CHECK(esp_timer_stop(stop_timer_));
        }
    }

    static void StopTimerCallback(void* arg) {
        auto* controller = static_cast<DualMotorController*>(arg);
        std::lock_guard<std::mutex> lock(controller->mutex_);
        controller->StopLocked();
    }

    ReturnValue DriveFor(int left_speed, int right_speed, int duration_ms) {
        std::lock_guard<std::mutex> lock(mutex_);
        StopTimer();
        DriveLocked(left_speed, right_speed);
        if (last_left_speed_ != 0 || last_right_speed_ != 0) {
            ESP_ERROR_CHECK(esp_timer_start_once(stop_timer_, Clamp(duration_ms, 1, kMaxDurationMs) * 1000));
        }
        return StateJson();
    }

    ReturnValue StopNow() {
        std::lock_guard<std::mutex> lock(mutex_);
        StopTimer();
        StopLocked();
        return StateJson();
    }

    ReturnValue DirectDriveFor(int left_direction, int right_direction, int duration_ms) {
        std::lock_guard<std::mutex> lock(mutex_);
        StopTimer();
        if (!direct_mode_) {
            StopPwmOutputs();
            ConfigureDirectOutputs();
            direct_mode_ = true;
        }
        ESP_ERROR_CHECK(gpio_set_level(standby_gpio_, 1));
        ApplyDirectMotor(left_in1_gpio_, left_in2_gpio_, Clamp(left_direction, -1, 1), left_reversed_);
        ApplyDirectMotor(right_in1_gpio_, right_in2_gpio_, Clamp(right_direction, -1, 1), right_reversed_);
        last_left_speed_ = Clamp(left_direction, -1, 1) * 100;
        last_right_speed_ = Clamp(right_direction, -1, 1) * 100;
        ESP_ERROR_CHECK(esp_timer_start_once(stop_timer_, Clamp(duration_ms, 1, kMaxDurationMs) * 1000));
        return StateJson();
    }

    std::string GetStateJson() {
        std::lock_guard<std::mutex> lock(mutex_);
        return StateJson();
    }

    std::string StateJson() const {
        return "{\"left_speed\":" + std::to_string(last_left_speed_) +
            ",\"right_speed\":" + std::to_string(last_right_speed_) +
            ",\"moving\":" + ((last_left_speed_ != 0 || last_right_speed_ != 0) ? "true" : "false") + "}";
    }

    void InitializePwm() {
        ledc_timer_config_t timer_config = {
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .duty_resolution = LEDC_TIMER_10_BIT,
            .timer_num = LEDC_TIMER_1,
            .freq_hz = kPwmFrequencyHz,
            .clk_cfg = LEDC_AUTO_CLK,
            .deconfigure = false,
        };
        ESP_ERROR_CHECK(ledc_timer_config(&timer_config));

        ledc_channel_config_t channel = {
            .gpio_num = left_in1_gpio_,
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = LEDC_CHANNEL_1,
            .intr_type = LEDC_INTR_DISABLE,
            .timer_sel = LEDC_TIMER_1,
            .duty = 0,
            .hpoint = 0,
            .flags = { .output_invert = 0 },
        };
        ESP_ERROR_CHECK(ledc_channel_config(&channel));
        channel.gpio_num = left_in2_gpio_;
        channel.channel = LEDC_CHANNEL_2;
        ESP_ERROR_CHECK(ledc_channel_config(&channel));
        channel.gpio_num = right_in1_gpio_;
        channel.channel = LEDC_CHANNEL_3;
        ESP_ERROR_CHECK(ledc_channel_config(&channel));
        channel.gpio_num = right_in2_gpio_;
        channel.channel = LEDC_CHANNEL_4;
        ESP_ERROR_CHECK(ledc_channel_config(&channel));
    }

    static PropertyList MotionProperties() {
        return PropertyList({
            Property("speed", kPropertyTypeInteger, kDefaultSpeed, 0, 100),
            Property("duration_ms", kPropertyTypeInteger, kDefaultDurationMs, 1, kMaxDurationMs),
        });
    }

public:
    DualMotorController(gpio_num_t left_in1_gpio, gpio_num_t left_in2_gpio,
                        gpio_num_t right_in1_gpio, gpio_num_t right_in2_gpio,
                        gpio_num_t standby_gpio, bool left_reversed, bool right_reversed)
        : left_in1_gpio_(left_in1_gpio), left_in2_gpio_(left_in2_gpio),
          right_in1_gpio_(right_in1_gpio), right_in2_gpio_(right_in2_gpio),
          standby_gpio_(standby_gpio), left_reversed_(left_reversed), right_reversed_(right_reversed) {
        ConfigureOutput(standby_gpio_);
        InitializePwm();
        esp_timer_create_args_t stop_timer_args = {
            .callback = &DualMotorController::StopTimerCallback,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "motor_stop",
        };
        ESP_ERROR_CHECK(esp_timer_create(&stop_timer_args, &stop_timer_));
        StopLocked();

        auto& mcp_server = McpServer::GetInstance();
        mcp_server.AddTool("self.chassis.go_forward", "控制终端完成前进动作，底层驱动由后端配置决定。必须直接调用，不要只文字回复或重复确认；速度和持续时间由后端持久化默认值注入，模型无需理解或传递底层参数，到时自动停止。",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(speed, speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.go_back", "控制终端完成后退动作，底层驱动由后端配置决定。必须直接调用，不要只文字回复或重复确认；速度和持续时间由后端持久化默认值注入，模型无需理解或传递底层参数，到时自动停止。",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(-speed, -speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.turn_left", "控制终端完成左转，底层使左轮后退、右轮前进。必须直接调用，不要只文字回复或重复确认；速度和持续时间由后端持久化默认值注入，模型无需理解或传递底层参数，到时自动停止。",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(-speed, speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.turn_right", "控制终端完成右转，底层使左轮前进、右轮后退。必须直接调用，不要只文字回复或重复确认；速度和持续时间由后端持久化默认值注入，模型无需理解或传递底层参数，到时自动停止。",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(speed, -speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.stop", "Immediately stop both chassis motors and disable the motor driver.",
            PropertyList(), [this](const PropertyList&) -> ReturnValue {
                return StopNow();
            });
    }
};

#endif  // __DUAL_MOTOR_CONTROLLER_H__
