"""Convert Intel Open Model Zoo road segmentation to BrakeCheck Core ML contract."""

import argparse
from pathlib import Path

import coremltools as ct
import cv2
import numpy as np
import onnx
from onnx import helper
import openvino as ov
import torch
import torch.nn.functional as F
from onnx2torch import convert


class RoadWrapper(torch.nn.Module):
    def __init__(self, network):
        super().__init__()
        self.network = network

    def forward(self, image):
        # BrakeCheck passes RGB image bytes. Intel's network expects BGR bytes.
        bgr = F.interpolate(image[:, [2, 1, 0]], size=(512, 896), mode="bilinear", align_corners=False)
        classes = self.network(bgr)
        if isinstance(classes, (list, tuple)):
            classes = classes[0]
        road = classes[:, 1:2] + classes[:, 3:4]
        road = F.interpolate(road, size=(384, 640), mode="bilinear", align_corners=False).clamp(0, 1)
        return torch.cat((1 - road, road), dim=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("onnx", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reference-xml", type=Path)
    parser.add_argument("--sample-image", type=Path)
    args = parser.parse_args()
    torch.set_num_threads(4)
    graph = onnx.load(str(args.onnx))
    # onnx2torch expects convolution weights as initializers. OpenVINO's
    # reverse converter emits them as Constant nodes instead.
    retained = []
    for node in graph.graph.node:
        if node.op_type == "Constant" and len(node.attribute) == 1 and node.attribute[0].type == onnx.AttributeProto.TENSOR:
            tensor = onnx.TensorProto()
            tensor.CopyFrom(node.attribute[0].t)
            tensor.name = node.output[0]
            graph.graph.initializer.append(tensor)
        else:
            retained.append(node)
    del graph.graph.node[:]
    graph.graph.node.extend(retained)
    onnx.checker.check_model(graph)
    model = RoadWrapper(convert(graph)).eval()
    sample = torch.rand(1, 3, 384, 640) * 255
    with torch.inference_mode():
        expected = model(sample)
        traced = torch.jit.trace(model, sample)
        torch.testing.assert_close(traced(sample), expected, rtol=.002, atol=.002)
        if args.reference_xml and args.sample_image:
            bgr = cv2.imread(str(args.sample_image))
            if bgr is None:
                raise ValueError(f"Cannot read {args.sample_image}")
            rgb = cv2.cvtColor(cv2.resize(bgr, (640, 384)), cv2.COLOR_BGR2RGB)
            input_tensor = torch.from_numpy(rgb.copy()).permute(2, 0, 1).unsqueeze(0).float()
            converted_road = model(input_tensor)[0, 1].numpy()
            native = ov.Core().compile_model(str(args.reference_xml), "CPU")
            bgr_input = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            bgr_input = cv2.resize(bgr_input, (896, 512)).transpose(2, 0, 1)[None].astype(np.float32)
            native_output = native([bgr_input])[native.output(0)][0]
            native_road = cv2.resize(native_output[1] + native_output[3], (640, 384))
            mae = float(np.abs(converted_road - native_road).mean())
            print(f"OpenVINO/PyTorch road MAE: {mae:.6f}", flush=True)
            if mae > .03:
                raise ValueError("Converted model disagrees with Intel reference")
    print("PyTorch road range", expected.min().item(), expected.max().item(), flush=True)
    coreml = ct.convert(traced,
        inputs=[ct.ImageType(name="image", shape=sample.shape, color_layout=ct.colorlayout.RGB, scale=1.0)],
        outputs=[ct.TensorType(name="road_scores")], convert_to="mlprogram",
        minimum_deployment_target=ct.target.iOS17, compute_precision=ct.precision.FLOAT16)
    coreml.user_defined_metadata.update({
        "source": "https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/road-segmentation-adas-0001",
        "license": "Apache-2.0",
        "preprocessing": "RGB bytes to BGR, resize 896x512",
        "decoder": "[1,2,384,640] non-road, road+mark probability",
    })
    coreml.save(str(args.output))
    print(f"Saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
