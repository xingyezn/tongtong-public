#pragma once

#include "face_tracking_controller.h"

#include <cstdint>

// Hardware-independent actuator interface. A real SG90 backend can translate
// angles to LEDC/MCPWM pulses without changing the tracking controller.
class FaceTrackingActuator {
public:
    virtual ~FaceTrackingActuator() = default;
    virtual void Apply(const FaceTrackingAngles& angles) = 0;
};

class SimulatedFaceTrackingActuator final : public FaceTrackingActuator {
public:
    void Apply(const FaceTrackingAngles& angles) override { last_angles_ = angles; }
    const FaceTrackingAngles& LastAngles() const { return last_angles_; }

    // SG90-compatible nominal conversion (500–2500us over 0–180 degrees).
    static uint32_t AngleToPulseUs(float angle_deg) {
        if (angle_deg < 0.0f) angle_deg = 0.0f;
        if (angle_deg > 180.0f) angle_deg = 180.0f;
        return static_cast<uint32_t>(500.0f + angle_deg * (2000.0f / 180.0f));
    }

private:
    FaceTrackingAngles last_angles_;
};
