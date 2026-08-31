#include "face_detection_test.h"

#include "dl_image_jpeg.hpp"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include <inttypes.h>
#include "human_face_detect.hpp"

namespace {
constexpr char kTag[] = "FaceDetectTest";
constexpr uint32_t kIterations = 10;
extern const uint8_t face_test_jpg_start[] asm("_binary_face_test_jpg_start");
extern const uint8_t face_test_jpg_end[] asm("_binary_face_test_jpg_end");
}

void RunFaceDetectionTest() {
    const size_t jpeg_size = static_cast<size_t>(face_test_jpg_end - face_test_jpg_start);
    dl::image::jpeg_img_t jpeg = {
        .data = const_cast<uint8_t*>(face_test_jpg_start),
        .data_len = jpeg_size,
    };
    auto image = dl::image::sw_decode_jpeg(jpeg, dl::image::DL_IMAGE_PIX_TYPE_RGB888);
    if (image.data == nullptr) {
        ESP_LOGE(kTag, "Failed to decode embedded test image (%u bytes)", jpeg_size);
        return;
    }

    ESP_LOGI(kTag, "Starting ESP-DL inference: %ux%u RGB888 (%u bytes)",
        image.width, image.height, jpeg_size);
    HumanFaceDetect detector;
    uint32_t total_ms = 0;
    uint32_t warm_ms = 0;
    for (uint32_t iteration = 0; iteration < kIterations; ++iteration) {
        const int64_t start_us = esp_timer_get_time();
        auto& results = detector.run(image);
        const uint32_t elapsed_ms = static_cast<uint32_t>((esp_timer_get_time() - start_us) / 1000);
        total_ms += elapsed_ms;
        if (iteration > 0) {
            warm_ms += elapsed_ms;
        }
        ESP_LOGI(kTag, "Inference %" PRIu32 "/%" PRIu32 ": %u face(s), %" PRIu32 " ms",
            iteration + 1, kIterations, static_cast<unsigned>(results.size()), elapsed_ms);
        if (iteration == 0) {
            for (const auto& result : results) {
                ESP_LOGI(kTag, "face score=%.4f box=(%d,%d)-(%d,%d)", result.score,
                    result.box[0], result.box[1], result.box[2], result.box[3]);
            }
        }
    }
    ESP_LOGI(kTag, "Average inference: total=%" PRIu32 " ms, warm=%" PRIu32 " ms (%" PRIu32 " runs)",
        total_ms / kIterations, warm_ms / (kIterations - 1), kIterations - 1);
    heap_caps_free(image.data);
}
