#pragma once

#include <string>

class Camera;

// Capture one frame and run the ESP-DL human face detector locally. The
// returned JSON contains source-image dimensions, elapsed time, and face boxes.
std::string CameraFaceDetectLocalJson(Camera* camera);
