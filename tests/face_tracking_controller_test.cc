#include "face_tracking_controller.h"
#include "face_tracking_actuator.h"

#include <cassert>
#include <cmath>

int main() {
    FaceTrackingController controller;
    SimulatedFaceTrackingActuator actuator;

    // A target at the image center must not move the servos.
    FaceTrackingBox center{140, 100, 180, 140, 0.9f};
    auto angles = controller.Update(&center, 320, 240, 0);
    assert(std::fabs(angles.pan_deg - 90.0f) < 0.01f);
    assert(std::fabs(angles.tilt_deg - 90.0f) < 0.01f);
    assert(angles.face_locked);

    // A target on the left/up moves both axes, but no more than max_step.
    FaceTrackingBox left_up{0, 0, 40, 40, 0.9f};
    angles = controller.Update(&left_up, 320, 240, 200);
    assert(angles.pan_deg > 90.0f && angles.pan_deg <= 96.0f);
    assert(angles.tilt_deg > 90.0f && angles.tilt_deg <= 96.0f);
    actuator.Apply(angles);
    assert(SimulatedFaceTrackingActuator::AngleToPulseUs(angles.pan_deg) > 1500);

    // A lost target is held for one second, then returns gradually.
    const auto held = controller.Update(nullptr, 320, 240, 1000);
    assert(std::fabs(held.pan_deg - angles.pan_deg) < 0.01f);
    const auto returning = controller.Update(nullptr, 320, 240, 1200);
    assert(returning.pan_deg < held.pan_deg);
    assert(!returning.face_locked);

    // Out-of-range targets are clamped to the configured servo limits.
    for (int i = 0; i < 40; ++i) controller.Update(&left_up, 320, 240, 1400 + i * 200);
    angles = controller.GetAngles();
    assert(angles.pan_deg >= 20.0f && angles.pan_deg <= 160.0f);
    assert(angles.tilt_deg >= 45.0f && angles.tilt_deg <= 135.0f);
    return 0;
}
