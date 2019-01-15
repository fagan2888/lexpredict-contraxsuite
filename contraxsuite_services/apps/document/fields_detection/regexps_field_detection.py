import io
from typing import Optional, List

import pandas as pd

from apps.document.field_types import FIELD_TYPES_REGISTRY, ValueExtractionHint, FieldType
from apps.document.fields_detection.fields_detection_abstractions import FieldDetectionStrategy, DetectedFieldValue, \
    ProcessLogger
from apps.document.fields_detection.stop_words import detect_with_stop_words_by_field_and_full_text
from apps.document.fields_processing.field_processing_utils import merge_document_field_values_to_python_value
from apps.document.models import ClassifierModel, TextUnit, DocumentFieldDetector
from apps.document.models import DocumentField, Document


class RegexpsOnlyFieldDetectionStrategy(FieldDetectionStrategy):
    code = DocumentField.VD_USE_REGEXPS_ONLY

    @classmethod
    def train_document_field_detector_model(cls,
                                            log: ProcessLogger,
                                            field: DocumentField,
                                            train_data_project_ids: Optional[List],
                                            use_only_confirmed_field_values: bool = False) -> Optional[ClassifierModel]:

        return None

    @classmethod
    def detect_field_values(cls,
                            log: ProcessLogger,
                            doc: Document,
                            field: DocumentField) -> List[DetectedFieldValue]:

        depends_on_full_text = doc.full_text
        detected_with_stop_words, detected_values = detect_with_stop_words_by_field_and_full_text(field,
                                                                                                  depends_on_full_text)
        if detected_with_stop_words:
            return detected_values or list()

        qs_text_units = TextUnit.objects \
            .filter(document=doc) \
            .filter(unit_type=field.text_unit_type) \
            .order_by('location_start', 'pk')

        field_detectors = DocumentFieldDetector.objects.filter(field=field)

        field_type_adapter = FIELD_TYPES_REGISTRY.get(field.type)  # type: FieldType

        detected_values = list()  # type: List[DetectedFieldValue]

        for text_unit in qs_text_units.iterator():

            for field_detector in field_detectors:
                matching_string = field_detector.matching_string(text_unit.text)
                if matching_string is not None:
                    value = field_detector.detected_value
                    hint_name = None
                    if field_type_adapter.value_aware:
                        hint_name = field_detector.extraction_hint or ValueExtractionHint.TAKE_FIRST.name
                        value, hint_name = field_type_adapter \
                            .get_or_extract_value(doc,
                                                  field, value,
                                                  hint_name,
                                                  matching_string)
                        if value is None:
                            continue

                    detected_values.append(DetectedFieldValue(field, value, text_unit, hint_name))

                    if not (field_type_adapter.multi_value or field.is_choice_field()):
                        break

            if detected_values and not (field_type_adapter.multi_value or field.is_choice_field()):
                break

        return detected_values


class FieldBasedRegexpsDetectionStrategy(FieldDetectionStrategy):
    code = DocumentField.VD_FIELD_BASED_REGEXPS

    @classmethod
    def uses_cached_document_field_values(cls, field):
        return True

    @classmethod
    def train_document_field_detector_model(cls,
                                            log: ProcessLogger,
                                            field: DocumentField,
                                            train_data_project_ids: Optional[List],
                                            use_only_confirmed_field_values: bool = False) -> Optional[ClassifierModel]:

        return None

    @classmethod
    def detect_field_values(cls,
                            log: ProcessLogger,
                            doc: Document,
                            field: DocumentField) -> List[DetectedFieldValue]:

        depends_on_fields = list(field.depends_on_fields.all())

        qs_document_field_values = doc.documentfieldvalue_set \
            .filter(removed_by_user=False) \
            .filter(field__in=depends_on_fields)

        field_code_to_value = merge_document_field_values_to_python_value(list(qs_document_field_values))

        field_code_to_value = {f.code: field_code_to_value.get(f.code) for f in depends_on_fields}

        if field.stop_words:
            depends_on_full_text = '\n'.join([str(v) for v in field_code_to_value.values()])
            detected_with_stop_words, detected_values \
                = detect_with_stop_words_by_field_and_full_text(field, depends_on_full_text)
            if detected_with_stop_words:
                return detected_values or list()

        field_detectors = DocumentFieldDetector.objects.filter(field=field)
        field_type_adapter = FIELD_TYPES_REGISTRY.get(field.type)  # type: FieldType

        detected_values = list()  # type: List[DetectedFieldValue]

        for depends_on_value in field_code_to_value.values():
            if not depends_on_value:
                continue
            depends_on_value = str(depends_on_value)
            for field_detector in field_detectors:
                matching_string = field_detector.matching_string(depends_on_value)
                if matching_string is not None:
                    value = field_detector.detected_value
                    hint_name = None
                    if field_type_adapter.value_aware:
                        hint_name = field_detector.extraction_hint or ValueExtractionHint.TAKE_FIRST.name
                        value, hint_name = field_type_adapter \
                            .get_or_extract_value(doc,
                                                  field, value,
                                                  hint_name,
                                                  matching_string)
                        if value is None:
                            continue

                    detected_values.append(DetectedFieldValue(field, value, None, hint_name))

                    if not (field_type_adapter.multi_value or field.is_choice_field()):
                        break

            if detected_values and not (field_type_adapter.multi_value or field.is_choice_field()):
                break

        return detected_values


FD_CATEGORY_IMPORTED_SIMPLE_CONFIG = 'imported_simple_config'


def apply_simple_config(log: ProcessLogger,
                        document_field: DocumentField,
                        csv: bytes,
                        drop_previous_field_detectors: bool,
                        update_field_choice_values: bool):
    df = pd.read_csv(io.BytesIO(csv), dtype=str)
    if df.shape[0] < 1 or df.shape[1] < 1:
        raise ValueError('Config csv contains no data')
    row_num = df.shape[0]

    if update_field_choice_values:
        choices = df[df.columns[0]].dropna().drop_duplicates().sort_values().tolist()
        document_field.choices = '\n'.join(choices)
        document_field.save()

    log.info('Creating {2} naive field detectors for document field {0} and document type {1}...'
             .format(document_field, document_field.document_type, df.shape[0]))
    log.set_progress_steps_number(int(row_num / 10) + 1)
    if drop_previous_field_detectors:
        DocumentFieldDetector.objects.filter(field=document_field, category=FD_CATEGORY_IMPORTED_SIMPLE_CONFIG).delete()
    for index, row in df.iterrows():
        detector = DocumentFieldDetector()
        detector.category = FD_CATEGORY_IMPORTED_SIMPLE_CONFIG
        detector.field = document_field
        detector.regexps_pre_process_lower = True
        detector.detected_value = row[0]
        detector.include_regexps = '\n'.join(row.dropna()).lower()
        detector.save()
        if index % 10 == 0:
            log.step_progress()
    log.info('Done.')
