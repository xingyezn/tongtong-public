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
    static constexpr int kDefaultSpeed = 60;
    static constexpr int kDefaultDurationMs = 1000;
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

    void DriveLocked(int left_speed, int right_speed) {
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
        SetPwmDuty(LEDC_CHANNEL_1, 0);
        SetPwmDuty(LEDC_CHANNEL_2, 0);
        SetPwmDuty(LEDC_CHANNEL_3, 0);
        SetPwmDuty(LEDC_CHANNEL_4, 0);
        ESP_ERROR_CHECK(gpio_set_level(standby_gpio_, 0));
        last_left_speed_ = 0;
        last_right_speed_ = 0;
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
        mcp_server.AddTool("self.chassis.get_state", "Get the chassis motor state. Motors are stopped after every motion command.",
            PropertyList(), [this](const PropertyList&) -> ReturnValue { return GetStateJson(); });

        mcp_server.AddTool("self.chassis.go_forward", "Drive forward for a limited time. The chassis stops automatically when the time expires.",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(speed, speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.go_back", "Drive backward for a limited time. The chassis stops automatically when the time expires.",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(-speed, -speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.turn_left", "Turn left in place for a limited time. The chassis stops automatically when the time expires.",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(-speed, speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.turn_right", "Turn right in place for a limited time. The chassis stops automatically when the time expires.",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(speed, -speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.spin", "Spin in place in a circle: the left and right wheels rotate in opposite directions. The chassis stops automatically when the time expires.",
            MotionProperties(), [this](const PropertyList& properties) -> ReturnValue {
                int speed = properties["speed"].value<int>();
                return DriveFor(speed, -speed, properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.drive", "Drive each wheel independently for a limited time. Speeds are -100 to 100; the chassis stops automatically when the time expires.",
            PropertyList({
                Property("left_speed", kPropertyTypeInteger, 0, -100, 100),
                Property("right_speed", kPropertyTypeInteger, 0, -100, 100),
                Property("duration_ms", kPropertyTypeInteger, kDefaultDurationMs, 1, kMaxDurationMs),
            }), [this](const PropertyList& properties) -> ReturnValue {
                return DriveFor(properties["left_speed"].value<int>(), properties["right_speed"].value<int>(),
                    properties["duration_ms"].value<int>());
            });
        mcp_server.AddTool("self.chassis.stop", "Immediately stop both chassis motors and disable the motor driver.",
            PropertyList(), [this](const PropertyList&) -> ReturnValue {
                return StopNow();
            });
    }
};

#endif  // __DUAL_MOTOR_CONTROLLER_H__
