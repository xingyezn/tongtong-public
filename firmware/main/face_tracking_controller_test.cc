#include "face_tracking_controller_test.h"

#include "esp_log.h"
#include "face_tracking_actuator.h"

#include <cmath>

namespace {
constexpr char kTag[] = "FaceTrackTest";
bool Near(float a, float b, float epsilon = 0.01f) {
    return std::fabs(a - b) < epsilon;
}
}

void RunFaceTrackingControllerTest() {
    FaceTrackingController controller;
    SimulatedFaceTrackingActuator actuator;
    bool passed = true;

    FaceTrackingBox center{140, 100, 180, 140, 0.9f};
    auto angles = controller.Update(&center, 320, 240, 0);
    passed &= Near(angles.pan_deg, 90.0f) && Near(angles.tilt_deg, 90.0f) && angles.face_locked;

    FaceTrackingBox left_up{0, 0, 40, 40, 0.9f};
    angles = controller.Update(&left_up, 320, 240, 200);
    passed &= angles.pan_deg > 90.0f && angles.pan_deg <= 96.0f;
    passed &= angles.tilt_deg > 90.0f && angles.tilt_deg <= 96.0f;
    actuator.Apply(angles);
    passed &= SimulatedFaceTrackingActuator::AngleToPulseUs(angles.pan_deg) > 1500;

    const auto held = controller.Update(nullptr, 320, 240, 1000);
    passed &= Near(held.pan_deg, angles.pan_deg);
    const auto returning = controller.Update(nullptr, 320, 240, 1300);
    passed &= returning.pan_deg < held.pan_deg && !returning.face_locked;

    for (int i = 0; i < 40; ++i) controller.Update(&left_up, 320, 240, 1400 + i * 200);
    angles = controller.GetAngles();
    passed &= angles.pan_deg >= 20.0f && angles.pan_deg <= 160.0f;
    passed &= angles.tilt_deg >= 45.0f && angles.tilt_deg <= 135.0f;

    if (passed) {
        ESP_LOGI(kTag, "Face tracking controller tests passed on ESP32");
    } else {
        ESP_LOGE(kTag, "Face tracking controller tests FAILED on ESP32");
    }
}
