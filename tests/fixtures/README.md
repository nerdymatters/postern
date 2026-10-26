# Face-detection test image

`selfie-face.jpg` is a square crop of the NASA portrait of astronaut Eileen Collins,
from [scikit-image v0.25.2's astronaut.png](https://github.com/scikit-image/scikit-image/blob/v0.25.2/skimage/data/astronaut.png).
[scikit-image documents the NASA photograph as public domain](https://scikit-image.org/docs/stable/api/skimage.data.html#skimage.data.astronaut).
This is a test fixture, not a staff enrollment or endorsement.

Source SHA-256: `88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5`.
Preparation: crop Pillow coordinates `(120, 10, 350, 240)`, resize to 512×512,
save as JPEG at quality 90. Group/tiny-face fixtures are composed in tests.
