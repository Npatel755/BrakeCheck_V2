import copy
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
import torch

from bcdata.lead_review import candidate_targets
from bcdata.lead_training import (LeadRanker, collate, geometry, internal_partition,
    metrics, picks, prepared_image, ranking_loss, validate_records)
from bcdata.lead_geometry_training import GeometryRanker, tensors


def record(identity='one', split='train', decision='box', missed=False):
    row = {'image_id': identity, 'source_sha256': identity, 'source': 'comma10k',
        'license_id': 'comma10k', 'pool': 'commercial', 'source_split': 'train', 'split': split,
        'session_id': identity, 'decision': decision, 'reviewer': 'human', 'revision': 1,
        'width': 100, 'height': 50, 'box': [10, 10, 30, 30] if decision == 'box' else None,
        'detections': [{'box': [60, 10, 80, 30] if missed else [10, 10, 30, 30],
                        'confidence': .8, 'forward_candidate': True}]}
    row['candidate_targets'] = candidate_targets(row, row['box'], decision)
    return row


class LeadTrainingTests(unittest.TestCase):
    def test_session_or_source_duplication_and_original_evaluation_are_rejected(self):
        first, second = record(), record('two', 'val')
        validate_records([first, second], Path('.'), False)
        for field in ['session_id', 'source_sha256', 'image_id']:
            changed = copy.deepcopy(second); changed[field] = first[field]
            with self.assertRaises(ValueError):
                validate_records([first, changed], Path('.'), False)
        second['source_split'] = 'val'
        with self.assertRaises(ValueError):
            validate_records([first, second], Path('.'), False)

    def test_unsigned_or_pending_annotations_cannot_train(self):
        for key, value in [('decision', 'skip'), ('revision', 0), ('reviewer', '')]:
            changed = record(); changed[key] = value
            with self.assertRaises(ValueError):
                validate_records([changed, record('two', 'val')], Path('.'), False)

    def test_export_targets_must_match_human_box_and_misses_are_not_negatives(self):
        missed = record(missed=True)
        self.assertFalse(missed['candidate_targets']['usable_for_ranking'])
        self.assertIsNone(missed['candidate_targets']['candidate_labels'])
        changed = record(); changed['candidate_targets']['candidate_labels'] = [0]
        with self.assertRaises(ValueError):
            validate_records([changed, record('two', 'val')], Path('.'), False)

    def test_letterbox_and_flip_transform_boxes_in_the_same_coordinate_system(self):
        image = Image.new('RGB', (100, 50))
        pixels, boxes = prepared_image(image, [[10, 10, 30, 30]])
        self.assertEqual(tuple(pixels.shape), (3, 192, 320))
        np.testing.assert_allclose(boxes[0], [.1, 48 / 192, .3, 112 / 192], atol=1e-6)
        _, mirrored = prepared_image(image, [[10, 10, 30, 30]], True)
        np.testing.assert_allclose(mirrored[0], [.7, 48 / 192, .9, 112 / 192], atol=1e-6)

    def test_no_lead_is_explicit_class_and_padding_cannot_win(self):
        pixels = torch.zeros(3, 192, 320)
        a = (pixels, torch.zeros(1, 4), torch.zeros(1, 12), torch.tensor([False]))
        b = (pixels, torch.zeros(2, 4), torch.zeros(2, 12), torch.tensor([True, False]))
        _, _, _, valid, target = collate([a, b])
        self.assertEqual(valid.tolist(), [[True, False], [True, True]])
        self.assertEqual(target.tolist(), [[False, False, True], [True, False, False]])
        self.assertTrue(torch.isfinite(ranking_loss(torch.zeros(2, 3), target)))
        self.assertEqual(picks([[.1, .9], [.8, .1, .1]], .85), [None, None])

    def test_metrics_include_detector_misses_and_false_acquisition(self):
        rows = [record(), record('missed', missed=True), record('negative', decision='no_lead')]
        measured = metrics(rows, [0, None, 0])
        self.assertAlmostEqual(measured['lead_recall'], .5)
        self.assertAlmostEqual(measured['decision_accuracy'], 1 / 3)
        self.assertEqual(measured['detector_missed_leads'], 1)
        self.assertEqual(measured['no_lead_false_acquisitions'], 1)

    def test_internal_partition_is_stable_and_contains_both_sides(self):
        self.assertEqual(internal_partition('session'), internal_partition('session'))
        self.assertEqual({internal_partition(str(i)) for i in range(100)}, {'fit', 'dev'})

    def test_real_model_masks_candidates_and_backpropagates_to_scene_encoder(self):
        torch.set_num_threads(2)
        model = LeadRanker()
        images = torch.rand(2, 3, 192, 320)
        boxes = torch.tensor([[[.2, .2, .4, .6], [.5, .3, .8, .8]]] * 2)
        geom = torch.rand(2, 2, 12)
        valid = torch.tensor([[True, False], [True, True]])
        logits = model(images, boxes, geom, valid)
        self.assertEqual(tuple(logits.shape), (2, 3))
        self.assertLess(float(logits[0, 1].detach()), -9000)
        ranking_loss(logits, torch.tensor([[True, False, False], [False, False, True]])).backward()
        self.assertGreater(float(model.encoder[0][0].weight.grad.abs().sum()), 0)

    def test_geometry_uses_exactly_the_visual_models_box_transform(self):
        row = record()
        pixels, boxes = prepared_image(Image.new('RGB', (100, 50)), [d['box'] for d in row['detections']])
        expected = geometry(boxes, row['detections'])
        values, valid, target = tensors([row])
        torch.testing.assert_close(values[0], expected)
        self.assertTrue(valid.all())

    def test_geometry_model_is_candidate_order_equivariant_and_padding_invariant(self):
        torch.manual_seed(7)
        model = GeometryRanker().eval()
        geom = torch.rand(2, 3, 12); valid = torch.ones(2, 3, dtype=torch.bool)
        with torch.no_grad():
            original = model(geom, valid)
            order = [2, 0, 1]
            shuffled = model(geom[:, order], valid)
            torch.testing.assert_close(shuffled[:, :3], original[:, order])
            torch.testing.assert_close(shuffled[:, -1], original[:, -1])
            padded = model(torch.cat([geom, torch.rand(2, 1, 12)], 1),
                           torch.cat([valid, torch.zeros(2, 1, dtype=torch.bool)], 1))
            torch.testing.assert_close(padded[:, :3], original[:, :3])
            torch.testing.assert_close(padded[:, -1], original[:, -1])
            self.assertTrue((padded[:, 3] < -9000).all())


if __name__ == '__main__':
    unittest.main()
