#include "camera_face_detect_local.h"

#include <cJSON.h>
#include <cstdint>
#include <string>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "linux/videodev2.h"

#include "esp32_camera.h"
#include "dl_image_jpeg.hpp"
#include "human_face_detect.hpp"
#include "image_to_jpeg.h"
#include "mbedtls/base64.h"

#define TAG "FaceDetectLocal"

namespace {

std::string Fail(const char* what) {
    ESP_LOGE(TAG, "%s", what);
    return std::string("local face detection failed: ") + what;
}

size_t AppendJpegChunk(void* arg, size_t, const void* data, size_t len) {
    auto* output = static_cast<std::string*>(arg);
    output->append(static_cast<const char*>(data), len);
    return len;
}

bool EncodeFrameToJpeg(Esp32Camera* camera, std::string* jpeg) {
    if (camera->frame_format() == V4L2_PIX_FMT_JPEG) {
        jpeg->assign(reinterpret_cast<const char*>(camera->frame_data()), camera->frame_length());
        return true;
    }
    return image_to_jpeg_cb(
        const_cast<uint8_t*>(camera->frame_data()), camera->frame_length(),
        camera->frame_width(), camera->frame_height(), camera->frame_format(), 80,
        AppendJpegChunk, jpeg);
}

bool AddBase64Jpeg(cJSON* out, const std::string& jpeg) {
    if (jpeg.empty()) {
        return false;
    }
    const size_t encoded_len = 4 * ((jpeg.size() + 2) / 3);
    std::string encoded(encoded_len + 1, '\0');
    size_t actual_len = 0;
    const int ret = mbedtls_base64_encode(
        reinterpret_cast<unsigned char*>(encoded.data()), encoded.size(), &actual_len,
        reinterpret_cast<const unsigned char*>(jpeg.data()), jpeg.size());
    if (ret != 0) {
        return false;
    }
    encoded.resize(actual_len);
    cJSON_AddStringToObject(out, "image_jpeg_base64", encoded.c_str());
    return true;
}

}  // namespace

std::string CameraFaceDetectLocalJson(Camera* camera) {
    auto* esp_cam = dynamic_cast<Esp32Camera*>(camera);
    if (esp_cam == nullptr) {
        return Fail("no supported camera");
    }
    if (!esp_cam->Capture()) {
        return Fail("camera capture error");
    }

    dl::image::img_t image{};
    bool image_owned = false;
    if (esp_cam->frame_format() == V4L2_PIX_FMT_JPEG) {
        dl::image::jpeg_img_t jpeg = {
            .data = const_cast<uint8_t*>(esp_cam->frame_data()),
            .data_len = esp_cam->frame_length(),
        };
        image = dl::image::sw_decode_jpeg(jpeg, dl::image::DL_IMAGE_PIX_TYPE_RGB888);
        image_owned = true;
    } else if (esp_cam->frame_format() == V4L2_PIX_FMT_RGB24) {
        image = {
            .data = const_cast<uint8_t*>(esp_cam->frame_data()),
            .width = esp_cam->frame_width(),
            .height = esp_cam->frame_height(),
            .pix_type = dl::image::DL_IMAGE_PIX_TYPE_RGB888,
        };
    } else {
        return Fail("unsupported camera frame format");
    }
    if (image.data == nullptr) {
        if (image_owned) {
            heap_caps_free(image.data);
        }
        return Fail("frame decode error");
    }

    const int64_t t0 = esp_timer_get_time();
    HumanFaceDetect detector(
        static_cast<HumanFaceDetect::model_type_t>(CONFIG_DEFAULT_HUMAN_FACE_DETECT_MODEL));
    auto& faces = detector.run(image);
    const int64_t elapsed_ms = (esp_timer_get_time() - t0) / 1000;

    cJSON* out = cJSON_CreateObject();
    cJSON_AddNumberToObject(out, "count", static_cast<int>(faces.size()));
    cJSON_AddNumberToObject(out, "width", static_cast<int>(image.width));
    cJSON_AddNumberToObject(out, "height", static_cast<int>(image.height));
    cJSON_AddNumberToObject(out, "elapsed_ms", static_cast<double>(elapsed_ms));
    std::string jpeg;
    if (EncodeFrameToJpeg(esp_cam, &jpeg)) {
        AddBase64Jpeg(out, jpeg);
    }
    cJSON* out_faces = cJSON_AddArrayToObject(out, "faces");
    for (const auto& face : faces) {
        cJSON* item = cJSON_CreateObject();
        cJSON* box = cJSON_CreateArray();
        cJSON_AddItemToArray(box, cJSON_CreateNumber(face.box[0]));
        cJSON_AddItemToArray(box, cJSON_CreateNumber(face.box[1]));
        cJSON_AddItemToArray(box, cJSON_CreateNumber(face.box[2]));
        cJSON_AddItemToArray(box, cJSON_CreateNumber(face.box[3]));
        cJSON_AddItemToObject(item, "box", box);
        cJSON_AddNumberToObject(item, "confidence", face.score);
        cJSON_AddItemToArray(out_faces, item);
    }

    char* text = cJSON_PrintUnformatted(out);
    std::string result = text ? text : std::string("{}");
    cJSON_free(text);
    cJSON_Delete(out);

    if (image_owned && image.data) {
        heap_caps_free(image.data);
    }
    ESP_LOGI(TAG, "local detect done: %s", result.c_str());
    return result;
}
