#ifndef __DUAL_MOTOR_CONTROLLER_H__
#define __DUAL_MOTOR_CONTROLLER_H__

#include <string>

#include <driver/gpio.h>
#include <driver/ledc.h>
#include <esp_err.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "mcp_server.h"

// A safe, open-loop controller for a TB6612FNG-compatible dual H-bridge.
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
    gpio_num_t left_pwm_gpio_;
    gpio_num_t right_in1_gpio_;
    gpio_num_t right_in2_gpio_;
    gpio_num_t right_pwm_gpio_;
    gpio_num_t standby_gpio_;
    bool left_reversed_;
    bool right_reversed_;
    int last_left_speed_ = 0;
    int last_right_speed_ = 0;

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

    void ApplyMotor(gpio_num_t in1_gpio, gpio_num_t in2_gpio, ledc_channel_t channel,
                    int speed, bool reversed) {
        speed = Clamp(speed, -100, 100);
        if (reversed) {
            speed = -speed;
        }

        if (speed > 0) {
            ESP_ERROR_CHECK(gpio_set_level(in1_gpio, 1));
            ESP_ERROR_CHECK(gpio_set_level(in2_gpio, 0));
        } else if (speed < 0) {
            ESP_ERROR_CHECK(gpio_set_level(in1_gpio, 0));
            ESP_ERROR_CHECK(gpio_set_level(in2_gpio, 1));
        } else {
            ESP_ERROR_CHECK(gpio_set_level(in1_gpio, 0));
            ESP_ERROR_CHECK(gpio_set_level(in2_gpio, 0));
        }
        SetPwmDuty(channel, speed < 0 ? -speed : speed);
    }

    void Drive(int left_speed, int right_speed) {
        left_speed = Clamp(left_speed, -100, 100);
        right_speed = Clamp(right_speed, -100, 100);
        if (left_speed == 0 && right_speed == 0) {
            Stop();
            return;
        }

        ESP_ERROR_CHECK(gpio_set_level(standby_gpio_, 1));
        ApplyMotor(left_in1_gpio_, left_in2_gpio_, LEDC_CHANNEL_1, left_speed, left_reversed_);
        ApplyMotor(right_in1_gpio_, right_in2_gpio_, LEDC_CHANNEL_2, right_speed, right_reversed_);
        last_left_speed_ = left_speed;
        last_right_speed_ = right_speed;
    }

    void Stop() {
        SetPwmDuty(LEDC_CHANNEL_1, 0);
        SetPwmDuty(LEDC_CHANNEL_2, 0);
        ESP_ERROR_CHECK(gpio_set_level(left_in1_gpio_, 0));
        ESP_ERROR_CHECK(gpio_set_level(left_in2_gpio_, 0));
        ESP_ERROR_CHECK(gpio_set_level(right_in1_gpio_, 0));
        ESP_ERROR_CHECK(gpio_set_level(right_in2_gpio_, 0));
        ESP_ERROR_CHECK(gpio_set_level(standby_gpio_, 0));
        last_left_speed_ = 0;
        last_right_speed_ = 0;
    }

    ReturnValue DriveFor(int left_speed, int right_speed, int duration_ms) {
        Drive(left_speed, right_speed);
        vTaskDelay(pdMS_TO_TICKS(Clamp(duration_ms, 1, kMaxDurationMs)));
        Stop();
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

        ledc_channel_config_t left_channel = {
            .gpio_num = left_pwm_gpio_,
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = LEDC_CHANNEL_1,
            .intr_type = LEDC_INTR_DISABLE,
            .timer_sel = LEDC_TIMER_1,
            .duty = 0,
            .hpoint = 0,
            .flags = { .output_invert = 0 },
        };
        ledc_channel_config_t right_channel = left_channel;
        right_channel.gpio_num = right_pwm_gpio_;
        right_channel.channel = LEDC_CHANNEL_2;
        ESP_ERROR_CHECK(ledc_channel_config(&left_channel));
        ESP_ERROR_CHECK(ledc_channel_config(&right_channel));
    }

    static PropertyList MotionProperties() {
        return PropertyList({
            Property("speed", kPropertyTypeInteger, kDefaultSpeed, 0, 100),
            Property("duration_ms", kPropertyTypeInteger, kDefaultDurationMs, 1, kMaxDurationMs),
        });
    }

public:
    DualMotorController(gpio_num_t left_in1_gpio, gpio_num_t left_in2_gpio, gpio_num_t left_pwm_gpio,
                        gpio_num_t right_in1_gpio, gpio_num_t right_in2_gpio, gpio_num_t right_pwm_gpio,
                        gpio_num_t standby_gpio, bool left_reversed, bool right_reversed)
        : left_in1_gpio_(left_in1_gpio), left_in2_gpio_(left_in2_gpio), left_pwm_gpio_(left_pwm_gpio),
          right_in1_gpio_(right_in1_gpio), right_in2_gpio_(right_in2_gpio), right_pwm_gpio_(right_pwm_gpio),
          standby_gpio_(standby_gpio), left_reversed_(left_reversed), right_reversed_(right_reversed) {
        ConfigureOutput(left_in1_gpio_);
        ConfigureOutput(left_in2_gpio_);
        ConfigureOutput(right_in1_gpio_);
        ConfigureOutput(right_in2_gpio_);
        ConfigureOutput(standby_gpio_);
        InitializePwm();
        Stop();

        auto& mcp_server = McpServer::GetInstance();
        mcp_server.AddTool("self.chassis.get_state", "Get the chassis motor state. Motors are stopped after every motion command.",
            PropertyList(), [this](const PropertyList&) -> ReturnValue { return StateJson(); });

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
                Stop();
                return StateJson();
            });
    }
};

#endif  // __DUAL_MOTOR_CONTROLLER_H__
