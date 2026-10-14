# N31 model licensing review

Status: not every bundled model has been cleared for commercial distribution.
This review does not change the N31 prototype IPA.

| Model | Evidence | Status |
| --- | --- | --- |
| RTMDet-tiny day | Exact weight card designates Apache-2.0; downloaded SHA-256 matches the card. | Commercial use permitted under the published license, with required notices and license compliance. |
| RTMDet-small low light | Exact weight card designates Apache-2.0; downloaded SHA-256 matches the card. | Commercial use permitted under the published license, with required notices and license compliance. |
| YOLOP lane/road | Converted metadata identifies official YOLOP checkpoint SHA-256 620e7495328cfc8e04807c14cbb8f8df9916953359d190cbf494b2d049f03b0c. Project license is MIT. Official project uses BDD100K; the dataset's commercial permissions are restricted. | Unresolved training-data/weight-use issue. Do not assume the MIT code license clears all commercial uses of pretrained weights. Obtain authoritative clarification/permission or replace with appropriately licensed weights. |
| CFV viewpoint | The publisher's official fort-cyber/Car-orientation-image weight page explicitly designates Apache-2.0. Its convnext-multi-weak.pth LFS SHA-256 is 26bd9e4eff8a4cfa3a264223ecced760f2fb3d1f29dc68f4e23b1b27babcd91d, matching the source hash in our converted model metadata. | Commercial use permitted under the publisher's stated Apache-2.0 terms, with required notices and license compliance. This new evidence supersedes the earlier weight-license uncertainty. |

Sources checked:
- https://huggingface.co/LibreYOLO/LibreRTMDett
- https://huggingface.co/LibreYOLO/LibreRTMDets
- https://www.apache.org/licenses/LICENSE-2.0
- https://github.com/hustvl/YOLOP
- https://github.com/hustvl/YOLOP/blob/main/LICENSE
- https://github.com/bdd100k/bdd100k/blob/master/doc/source/license.rst
- https://github.com/bdd100k/bdd100k/discussions/130
- https://github.com/fort-cyber/car-orientation
- https://huggingface.co/fort-cyber/Car-orientation-image

The BDD100K dataset license distinguishes noncommercial uses from commercial
permissions for members/affiliates and separate licensing opportunities. How
that applies to this downstream pretrained checkpoint requires clarification;
this review does not assert that a dataset license automatically prohibits all
inference using a model trained on it.

Apache/MIT generally permit commercial and closed-source distribution subject
to their conditions. Preserve copyright/license/NOTICE material and identify
modifications as applicable. N31 packages model license texts and conversion
notices, but those notices alone cannot resolve unclear upstream permissions.
The old Ultralytics vehicle detectors are excluded from N31. That replacement
does not by itself establish commercial readiness of the whole app.
