"""Write the human-readable first lead-model experiment report after verification."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / 'BrakeCheck_Dataset/experiments/lead_cars'
OUT = ROOT / 'reports/lead-cars-training-20261024'
audit = json.loads((OUT / 'verification.json').read_text())
names = ['mnv3s-scratch-20261024-01', 'geometry-20261024-01']
labels = ['MobileNetV3 scene + ROI', 'Geometry MLP']
data = [json.loads((BASE / 'runs' / name / 'metrics.json').read_text()) for name in names]
histories = [json.loads((BASE / 'runs' / name / 'history.json').read_text()) for name in names]

fig, axes = plt.subplots(1, 2, figsize=(11, 4), constrained_layout=True)
for name, label, history in zip(names, labels, histories):
    epochs = [r['epoch'] for r in history]
    axes[0].plot(epochs, [r['dev_loss'] for r in history], label=label)
    axes[1].plot(epochs, [100 * r['dev_accuracy'] for r in history], label=label)
axes[0].set(xlabel='Epoch', ylabel='Internal development loss')
axes[1].set(xlabel='Epoch', ylabel='Internal development decisions correct (%)', ylim=(0, 100))
for ax in axes:
    ax.grid(alpha=.25); ax.legend()
fig.suptitle('Lead scoring pilot — internal development only; 94-session validation excluded')
fig.savefig(OUT / 'development-curves.png', dpi=160)
plt.close(fig)

table = []
for label, measured in zip(labels, data):
    all_data, supported = measured['validation_all'], measured['validation_detector_supported']
    table.append(f"| {label} | {all_data['correct_decisions']}/94 ({all_data['decision_accuracy']:.1%}) | "
                 f"{supported['correct_decisions']}/72 ({supported['decision_accuracy']:.1%}) | "
                 f"{all_data['correct_lead_selections']}/66 | {all_data['no_lead_false_acquisitions']}/28 |")
selected = audit['selected_candidate']
report = f'''# First lead-model training run — 2026-10-24

Training completed on the user's RTX 4090. All 500 saved reviews were frozen:
362 human lead boxes and 138 explicit no-lead decisions. No pending or skipped
photo became a training negative. No external dataset or pretrained weights
were downloaded or used. This run does not change the iPhone app.

## Models and split

- MobileNetV3-Small scene encoder, four-sample ROI pooling, global context,
  geometry head and explicit no-lead head: 1,113,250 parameters, trained from
  scratch for 80 epochs. Full scene encoder runs once per image.
- Geometry-only set MLP comparator: 11,266 parameters, 250 CPU epochs.
  This learned model uses candidate coordinates/confidence and pooled candidate
  context, but cannot see road pixels, lane curves or vehicle direction.

The fixed original lead split is 406 train / 94 validation sessions. Of the
406 training photos, 267 have usable detector targets. A label-independent
recording-session hash assigns 222 to fitting and 45 to internal development.
The other 139 human leads have no candidate matching at IoU >= 0.5. They remain
available for detector improvement and were never mislabeled as no-lead.

The 94-session split was excluded from gradients, epoch selection and threshold
tuning. Both experiments used identical fit/dev/validation identities. Model
and abstention thresholds were selected on internal development, then evaluated
on original validation. The selected experimental candidate is **{selected}**,
chosen by minimum internal-development cross entropy, not by validation accuracy.

## Original held-out results

Correct means selecting a candidate overlapping the human lead by IoU >= 0.5,
or abstaining on an explicitly labeled no-lead image. The full 94-image result
includes 22 detector-missed leads. The 72-image conditional result excludes
those misses and must not be presented as full system accuracy.

| Model | All held-out decisions | Detector-supported decisions | Correct lead selections | False acquisitions on no-lead images |
| --- | --- | --- | --- | --- |
{chr(10).join(table)}

Both models are experimental. In particular, the geometry scorer still falsely
selected a lead in 6 of 28 no-lead images. Detector coverage is another limit:
44 of 66 held-out positive leads have a matching candidate. A scorer cannot
recover a vehicle absent from the detector candidates. The separate static
geometry proxy in the visual run's metrics is not the full iPhone algorithm.

## Artifacts and checks

- [Verification and model comparison](verification.json)
- [Internal development curves](development-curves.png)
- [Studio status checks](studio-verification.json)
- [Visual training log](train.log), [geometry training log](geometry-train.log)
- [MobileNet checkpoint](../../BrakeCheck_Dataset/experiments/lead_cars/runs/mnv3s-scratch-20261024-01/best.pt)
- [Compact geometry TorchScript model](../../BrakeCheck_Dataset/experiments/lead_cars/runs/geometry-20261024-01/lead-geometry.torchscript.pt)
- [Geometry checkpoint and metadata](../../BrakeCheck_Dataset/experiments/lead_cars/runs/geometry-20261024-01/provenance.json)

32 focused tests passed. They cover reviewed-only targets, original-evaluation
exclusion, duplicate/session leakage, box coordinate transforms, model gradients,
candidate padding/order invariance, detection-miss accounting and worker status.
All 500 original image hashes and the original dataset's protected validation/test
sessions and hashes were rechecked. Both trained source snapshots and checkpoints
match their recorded hashes. The compact TorchScript graph matches the eager
checkpoint across batch/candidate sizes, with maximum logit difference
{audit['torchscript_max_logit_error']:.3g}. Desktop/phone Studio checks found no
JavaScript errors or label writes.

These are selected still images, not a driving benchmark. Temporal switching,
wrong-lead persistence, sparse scheduling and sustained iPhone latency/thermals
remain untested. Neither experimental model is installed in the phone app.

External dataset verification is recorded in the
[preannotated-data shortlist](../lead-cars-20261024/preannotated-datasets.md):
SUBARU published terms are noncommercial/competition-only; SemanticSpray's original
data terms remain unverified while its repository service is unavailable.
'''
(OUT / 'README.md').write_text(report, encoding='utf-8')
print('Wrote training report and development-curves.png')
