"""Inspect permissive RTMDet checkpoints without touching app resources."""
from pathlib import Path
import json
import torch
from libreyolo import LibreYOLO

torch.set_num_threads(4)
out = Path(__file__).resolve().parent / "artifacts"
out.mkdir(exist_ok=True)
for size in ("t", "s"):
    detector = LibreYOLO(f"LibreRTMDet{size}.pt", device="cpu")
    net = detector.model.eval()
    net.head.export = True
    print(size, type(net), sum(p.numel() for p in net.parameters()), flush=True)
    print(net.head, flush=True)
    with torch.no_grad():
        result = net(torch.zeros(1, 3, 640, 640))
    print(type(result), [r.shape for r in result] if isinstance(result, tuple) else result.shape, flush=True)
    print(detector.__dict__.keys(), flush=True)
