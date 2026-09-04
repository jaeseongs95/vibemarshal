from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    EvidenceKind, IntegrationValidationContract, ValidationContract, ValidationExecutionStep,
)
from flowmarshal.engine.planner_roles import PlanExpansionDraft
from flowmarshal.engine.roles import strict_json_output_schema
from tests.test_engine_role_adapters import _plan_response


class EvidenceContractTests(unittest.TestCase):
    def test_live_failure_names_are_rejected_before_plan_review(self):
        # A4 실제 호출에서 admission된 미지원 이름. 판정 결과에 맞춘 alias는 만들지 않는다.
        for name in ('scoped_change_evidence', 'public_contract_evidence', 'test_result',
                     'assertion_result', 'semantic_review', 'contract_evidence',
                     'independent_goal_test_result', 'final_goal_verdict'):
            for location in ('task', 'goal'):
                with self.subTest(name=name, location=location):
                    payload = _plan_response()
                    contract = (payload['tasks'][0]['validations'][0] if location == 'task'
                                else payload['integration_validations'][0])
                    contract['required_evidence_kinds'] = [name]
                    with self.assertRaisesRegex(ValidationError, '알 수 없는 evidence kind'):
                        PlanExpansionDraft.model_validate_json(json.dumps(payload))

    def test_supported_strings_keep_canonical_digest_and_strict_types(self):
        for kind in EvidenceKind:
            with self.subTest(kind=kind):
                payload = dict(validation_id='validation_one', statement='직접 증거를 검사한다.',
                               method='deterministic', required_evidence_kinds=(kind.value,))
                contract = ValidationContract(**payload)
                self.assertEqual(str, type(contract.required_evidence_kinds[0]))
                self.assertEqual(sha256_digest(payload), sha256_digest(contract))
                self.assertEqual(contract, ValidationContract.model_validate_json(contract.model_dump_json()))
        for kinds in ((123,), ('test', 'test'), ('unknown',)):
            with self.subTest(kinds=kinds), self.assertRaises(ValidationError):
                ValidationContract(**(payload | {'required_evidence_kinds': kinds}))

    def test_provider_schema_exposes_the_same_enum_at_all_boundaries(self):
        expected = [kind.value for kind in EvidenceKind]
        for model in (ValidationContract, IntegrationValidationContract, ValidationExecutionStep):
            with self.subTest(model=model):
                schema = strict_json_output_schema(model.model_json_schema())
                self.assertEqual(expected, schema['properties']['required_evidence_kinds']['items']['enum'])
        schema = strict_json_output_schema(PlanExpansionDraft.model_json_schema())
        for name in ('ValidationContract', 'IntegrationValidationContract'):
            self.assertEqual(expected, schema['$defs'][name]['properties']['required_evidence_kinds']['items']['enum'])

    def test_manual_execution_step_cannot_introduce_an_unknown_kind(self):
        with self.assertRaisesRegex(ValidationError, '알 수 없는 evidence kind'):
            ValidationExecutionStep(validation_id='validation_one', method='deterministic',
                argv=('python', '-m', 'unittest'), working_directory='.',
                required_evidence_kinds=('test_result',))


if __name__ == '__main__':
    unittest.main()
