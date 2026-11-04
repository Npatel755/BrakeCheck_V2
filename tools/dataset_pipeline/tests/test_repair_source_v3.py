import json
from pathlib import Path

import pytest

from bcdata.repair_source_v3 import verify_regression_report
from bcdata.teachers.base import file_sha256
import bcdata.source_converter_v3 as converter


def evidence():
    return {'converter_code_sha256':file_sha256(Path(converter.__file__)),
            'safety_checks':{'approved_sample_regressions':0,'original_annotations_unchanged':True,
                             'converter_and_dependencies_stable':True,'source_fingerprints_verified':True},
            'dependency_sha256':{}}


def test_application_requires_zero_known_annotation_regressions(tmp_path):
    report=evidence();path=tmp_path/'report.json'
    path.write_text(json.dumps(report))
    assert verify_regression_report(path) == report
    report['safety_checks']['approved_sample_regressions']=1
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='unresolved annotation regressions'):
        verify_regression_report(path)


def test_application_rejects_stale_converter_or_dependency(tmp_path):
    report=evidence();path=tmp_path/'report.json'
    report['converter_code_sha256']='a'*64
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='different converter'):
        verify_regression_report(path)
    report=evidence();dependency=tmp_path/'dependency.py';dependency.write_text('old')
    report['dependency_sha256'][str(dependency)]=file_sha256(dependency)
    dependency.write_text('changed')
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='dependency changed'):
        verify_regression_report(path)
