import copy
import unittest
from pathlib import Path

import numpy as np
import torch

from bcdata.lead_detector_training import (LeadDetectionDataset, average_precision, detector_metrics,
    initialize, letterbox, normalize, partitions, restore_boxes, select_threshold)


def row(identity, decision='box', split='train'):
    return {'image_id': identity, 'session_id': identity, 'split': split,
            'decision': decision, 'box': [10., 10., 30., 30.] if decision == 'box' else None}


class LeadDetectorTrainingTests(unittest.TestCase):
    def test_previously_unmatched_boxes_are_used_without_a_detector_candidate(self):
        from bcdata.lead_training import internal_partition
        rows = [row(str(i)) for i in range(30)] + [row('held-out', split='val')]
        for r in rows:
            r['candidate_targets'] = {'detector_missed_lead': True, 'usable_for_ranking': False}
        fit, dev, val = partitions(rows)
        self.assertEqual(len(fit) + len(dev), 30)
        self.assertEqual([r['image_id'] for r in val], ['held-out'])
        self.assertTrue(all(internal_partition(r['session_id']) == 'fit' for r in fit))
        leaked = copy.deepcopy(rows); leaked[-1]['session_id'] = rows[0]['session_id']
        with self.assertRaises(ValueError):
            partitions(leaked)

    def test_box_roundtrip_including_horizontal_flip_and_aspect_padding(self):
        image = np.zeros((71, 119, 3), np.uint8)
        box = [11., 13., 51., 47.]
        pixels, gt, affine = letterbox(image, box)
        self.assertEqual(tuple(pixels.shape), (3, 640, 640))
        torch.testing.assert_close(restore_boxes(gt, affine, 119, 71), torch.tensor([box]), atol=1e-5, rtol=1e-5)
        _, flipped, affine = letterbox(image, box, True)
        torch.testing.assert_close(restore_boxes(flipped, affine, 119, 71),
                                   torch.tensor([[68., 13., 108., 47.]]), atol=1e-5, rtol=1e-5)
        _, empty, _ = letterbox(image)
        self.assertEqual(tuple(empty.shape), (0, 4))

    def test_ap_penalizes_wrong_lane_negative_images_and_duplicate_detections(self):
        rows = [row('lead'), row('negative', 'no_lead')]
        correct = {'box': [10., 10., 30., 30.], 'confidence': .8}
        self.assertAlmostEqual(average_precision(rows, [[correct], []]), 1.)
        false = {**correct, 'confidence': .9}
        self.assertAlmostEqual(average_precision(rows, [[correct], [false]]), .5)
        measured = detector_metrics(rows, [[correct], [false]], .28)
        self.assertEqual(measured['covered_leads'], 1)
        self.assertEqual(measured['no_lead_false_acquisitions'], 1)
        self.assertEqual(measured['correct_decisions'], 1)

    def test_coverage_does_not_hide_wrong_top_prediction(self):
        rows = [row('lead'), row('none', 'no_lead')]
        predictions = [[{'box': [50., 50., 70., 70.], 'confidence': .9},
                        {'box': [10., 10., 30., 30.], 'confidence': .8}], []]
        measured = detector_metrics(rows, predictions, .28)
        self.assertEqual(measured['covered_leads'], 1)
        self.assertEqual(measured['correct_top1_leads'], 0)
        self.assertEqual(measured['decision_accuracy'], .5)

    def test_threshold_is_selected_from_explicit_negative_and_positive_examples(self):
        rows = [row('lead'), row('none', 'no_lead')]
        predictions = [[{'box': [10., 10., 30., 30.], 'confidence': .8}],
                       [{'box': [50., 50., 70., 70.], 'confidence': .2}]]
        threshold = select_threshold(rows, predictions)
        self.assertGreater(threshold, .2); self.assertLessEqual(threshold, .8)

    def test_real_rtmdet_partial_transfer_has_finite_gradients_for_lead_and_no_lead(self):
        # The review environment intentionally omits this model dependency.
        # Keep the real-gradient check active in the separate model environment.
        try:
            import libreyolo
        except ModuleNotFoundError as error:
            if error.name != 'libreyolo':
                raise
            self.skipTest('Requires LibreYOLO; run this case in .gpu-venv')
        from libreyolo.models.rtmdet.loss import RTMDetLoss
        root = Path(__file__).resolve().parents[3]
        torch.set_num_threads(2)
        model = initialize(root / 'weights/LibreRTMDett.pt', 'cpu')
        self.assertEqual(model.head.num_classes, 1)
        self.assertFalse(any(p.requires_grad for p in model.backbone.parameters()))
        model.train()
        for module in model.modules():
            if isinstance(module, torch.nn.BatchNorm2d): module.eval()
        images = normalize(torch.full((2, 3, 128, 128), 114.))
        cls, reg = model(images)
        criterion = RTMDetLoss(1, distributed_normalize=False)
        for boxes in [[torch.tensor([[30., 30., 70., 80.]]), torch.empty(0, 4)],
                      [torch.empty(0, 4), torch.empty(0, 4)]]:
            model.zero_grad(set_to_none=True)
            cls, reg = model(images)
            loss = criterion(cls, reg, boxes, [torch.zeros(len(b), dtype=torch.long) for b in boxes])['total_loss']
            self.assertTrue(torch.isfinite(loss))
            loss.backward()
            self.assertTrue(all(p.grad is None or torch.isfinite(p.grad).all() for p in model.parameters()))
            self.assertGreater(float(model.head.rtm_cls[0].weight.grad.abs().sum()), 0)


if __name__ == '__main__':
    unittest.main()
