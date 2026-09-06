#include "face_tracking_controller.h"

#include <cmath>

FaceTrackingController::FaceTrackingController(const FaceTrackingConfig& config)
    : config_(config) {
    Reset();
}

float FaceTrackingController::Clamp(float value, float low, float high) {
    return value < low ? low : (value > high ? high : value);
}

float FaceTrackingController::LimitStep(float current, float target, float max_step) {
    const float delta = target - current;
    if (delta > max_step) return current + max_step;
    if (delta < -max_step) return current - max_step;
    return target;
}

float FaceTrackingController::MoveToCenter(float current, float center, float step) const {
    return LimitStep(current, center, step);
}

void FaceTrackingController::Reset() {
    angles_.pan_deg = Clamp(config_.pan_center_deg, config_.pan_min_deg, config_.pan_max_deg);
    angles_.tilt_deg = Clamp(config_.tilt_center_deg, config_.tilt_min_deg, config_.tilt_max_deg);
    angles_.face_locked = false;
    last_face_ms_ = 0;
    has_face_timestamp_ = false;
}

FaceTrackingAngles FaceTrackingController::Update(const FaceTrackingBox* box, int image_width,
                                                  int image_height, uint32_t now_ms) {
    const bool valid_box = box != nullptr && image_width > 0 && image_height > 0 &&
                           box->x2 > box->x1 && box->y2 > box->y1;
    if (valid_box) {
        const float face_x = (static_cast<float>(box->x1) + static_cast<float>(box->x2)) * 0.5f;
        const float face_y = (static_cast<float>(box->y1) + static_cast<float>(box->y2)) * 0.5f;
        const float error_x = 0.5f - face_x / static_cast<float>(image_width);
        const float error_y = 0.5f - face_y / static_cast<float>(image_height);
        const float pan_target = angles_.pan_deg +
            (std::fabs(error_x) > config_.dead_zone ? config_.pan_kp * error_x : 0.0f);
        const float tilt_target = angles_.tilt_deg +
            (std::fabs(error_y) > config_.dead_zone ? config_.tilt_kp * error_y : 0.0f);
        angles_.pan_deg = Clamp(LimitStep(angles_.pan_deg, pan_target, config_.max_step_deg),
                                config_.pan_min_deg, config_.pan_max_deg);
        angles_.tilt_deg = Clamp(LimitStep(angles_.tilt_deg, tilt_target, config_.max_step_deg),
                                 config_.tilt_min_deg, config_.tilt_max_deg);
        angles_.face_locked = true;
        last_face_ms_ = now_ms;
        has_face_timestamp_ = true;
        return angles_;
    }

    angles_.face_locked = false;
    if (has_face_timestamp_ && now_ms - last_face_ms_ >= config_.lost_timeout_ms) {
        angles_.pan_deg = MoveToCenter(angles_.pan_deg, config_.pan_center_deg, config_.return_step_deg);
        angles_.tilt_deg = MoveToCenter(angles_.tilt_deg, config_.tilt_center_deg, config_.return_step_deg);
    }
    return angles_;
}
