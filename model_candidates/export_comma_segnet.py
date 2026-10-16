"""Convert pinned comma10k-segnet weights to Core ML for an iPhone trial."""

import json
from pathlib import Path

import coremltools as ct
import segmentation_models_pytorch as smp
from safetensors.torch import load_file
import torch


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "comma10k-segnet"
DESTINATION = HERE / "CommaLane.mlpackage"


def main():
    torch.set_num_threads(4)
    config = json.loads((SOURCE / "config.json").read_text())
    config.pop("_model_class")
    model = smp.Unet(**config).eval()
    model.load_state_dict(load_file(SOURCE / "model.safetensors"), strict=True)
    sample = torch.zeros(1, 3, 384, 512)
    with torch.inference_mode():
        expected = model(sample)
        traced = torch.jit.trace(model, sample, check_trace=False)
        torch.testing.assert_close(traced(sample), expected, rtol=.001, atol=.001)
    converted = ct.convert(
        traced,
        inputs=[ct.ImageType(name="image", shape=sample.shape,
                             color_layout=ct.colorlayout.RGB, scale=1.0)],
        outputs=[ct.TensorType(name="lane_scores")],
        convert_to="mlprogram", minimum_deployment_target=ct.target.iOS17,
        compute_precision=ct.precision.FLOAT16,
    )
    converted.user_defined_metadata.update({
        "source": "https://huggingface.co/commaai/comma10k-segnet",
        "source_revision": "b642c614eb0737fe7befa229f47fbd0245033038",
        "source_sha256": "8208672861ad1b111dc98f3a7c54196d29875b709c7353e2dd1b7614343fb3a8",
        "license": "MIT",
        "preprocessing": "resize RGB to 512x384; raw byte values; no normalization",
        "decoder": "[1,5,384,512] semantic logits; class mapping requires visual validation",
    })
    converted.save(str(DESTINATION))
    print(f"Saved {DESTINATION}")


if __name__ == "__main__":
    main()
