#pragma once
#include "sdkconfig.h"

#ifndef CONFIG_IDF_TARGET_ESP32
#include <lvgl.h>
#include <thread>
#include <memory>
#include <vector>

#include <freertos/FreeRTOS.h>
#include "camera.h"
#include "esp_video_init.h"
#include "linux/videodev2.h"

// Keep the camera format type available without pulling in the software JPEG
// conversion header, which is disabled for the JPEG passthrough build.
typedef uint32_t v4l2_pix_fmt_t;

class Esp32Camera : public Camera {
private:
    struct FrameBuffer {
        uint8_t *data = nullptr;
        size_t len = 0;
        uint16_t width = 0;
        uint16_t height = 0;
        v4l2_pix_fmt_t format = 0;
    } frame_;
    v4l2_pix_fmt_t sensor_format_ = 0;
#ifdef CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    uint16_t sensor_width_ = 0;
    uint16_t sensor_height_ = 0;
#endif  // CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    int video_fd_ = -1;
    bool streaming_on_ = false;
    struct MmapBuffer { void *start = nullptr; size_t length = 0; };
    std::vector<MmapBuffer> mmap_buffers_;
    std::string explain_url_;
    std::string explain_token_;

public:
    Esp32Camera(const esp_video_init_config_t& config);
    ~Esp32Camera();

    virtual void SetExplainUrl(const std::string& url, const std::string& token);
    virtual bool Capture();
    bool ProbeFrame();
    // Accessors for on-device vision processing of the last captured frame.
    const uint8_t* frame_data() const { return frame_.data; }
    size_t frame_length() const { return frame_.len; }
    uint16_t frame_width() const { return frame_.width; }
    uint16_t frame_height() const { return frame_.height; }
    v4l2_pix_fmt_t frame_format() const { return frame_.format; }
    // 翻转控制函数
    virtual bool SetHMirror(bool enabled) override;
    virtual bool SetVFlip(bool enabled) override;
    virtual std::string Explain(const std::string& question);
};

#endif // ndef CONFIG_IDF_TARGET_ESP32
