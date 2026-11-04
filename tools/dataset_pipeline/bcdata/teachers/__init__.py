"""Annotation teachers. Build them with `make_teacher(name, settings, registry)`."""
from __future__ import annotations


def make_teacher(name: str, settings, registry):
    if name == "source_labels":
        from .source_labels import SourceLabelTeacher
        return SourceLabelTeacher(settings, registry)
    if name == "comma_segnet":
        from .comma_segnet import CommaSegnetTeacher
        return CommaSegnetTeacher(settings)
    if name == "sam3":
        from .sam3_teacher import Sam3Teacher
        return Sam3Teacher(settings)
    if name == "sam2_refine":
        from .sam2_refine import Sam2RefineTeacher
        return Sam2RefineTeacher(settings, registry)
    if name == "dinov3_seg":
        from .dinov3_seg import Dinov3SegTeacher
        return Dinov3SegTeacher(settings, registry)
    raise KeyError(f"unknown teacher {name!r}")
