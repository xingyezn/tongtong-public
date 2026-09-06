#pragma once

#include <cstdint>

// Software-only pan/tilt controller. It does not access GPIO or a servo driver;
// callers apply the returned angles to their selected actuator backend.
struct FaceTrackingBox {
    int x1 = 0;
    int y1 = 0;
    int x2 = 0;
    int y2 = 0;
    float score = 0.0f;
};

struct FaceTrackingAngles {
    float pan_deg = 90.0f;
    float tilt_deg = 90.0f;
    bool face_locked = false;
};

struct FaceTrackingConfig {
    float pan_center_deg = 90.0f;
    float tilt_center_deg = 90.0f;
    float pan_min_deg = 20.0f;
    float pan_max_deg = 160.0f;
    float tilt_min_deg = 45.0f;
    float tilt_max_deg = 135.0f;
    float pan_kp = 35.0f;
    float tilt_kp = 30.0f;
    float dead_zone = 0.05f;
    float max_step_deg = 6.0f;
    float return_step_deg = 2.0f;
    uint32_t lost_timeout_ms = 1000;
};

class FaceTrackingController {
public:
    explicit FaceTrackingController(const FaceTrackingConfig& config = FaceTrackingConfig());

    FaceTrackingAngles Update(const FaceTrackingBox* box, int image_width, int image_height,
                              uint32_t now_ms);
    void Reset();
    FaceTrackingAngles GetAngles() const { return angles_; }

private:
    static float Clamp(float value, float low, float high);
    static float LimitStep(float current, float target, float max_step);
    float MoveToCenter(float current, float center, float step) const;

    FaceTrackingConfig config_;
    FaceTrackingAngles angles_;
    uint32_t last_face_ms_ = 0;
    bool has_face_timestamp_ = false;
};
