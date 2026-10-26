# Selfie face detector

`face_detection_yunet_2023mar.onnx` is the YuNet v2 face detector from
[OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet).
It is distributed under the accompanying MIT license, `YUNET-LICENSE.txt`.

- Upstream commit: `f12e12798e8314f7c074a6656816c048dcc95b7a`
- Size: 232,589 bytes (the model itself, not a Git LFS pointer)
- SHA-256: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`
- Runtime: OpenCV headless 4.x, CPU only, one thread per converter process.

The bundled model is checked against this hash before inference. Production never
downloads a model or sends an image to an inference service. The bounded converter
checks the EXIF-oriented, square-cropped 256×256 portrait before saving it. At least
0.85 detector confidence and exactly one face are required. Its shorter dimension
must fill at least 25% of the square; the face box must fit with a 5% tolerance and
all five landmarks must lie inside the image. These are usability thresholds, not
proof of identity, liveness, or authenticity. Printed/screen faces can pass.

Only the sanitized portrait is retained; bounding boxes, scores and landmarks are
discarded with the converter process. There is no embedding or face comparison.
Model updates require an explicit version/hash change and detector regression tests.
