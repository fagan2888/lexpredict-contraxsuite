"""
    Copyright (C) 2017, ContraxSuite, LLC

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

    You can also be released from the requirements of the license by purchasing
    a commercial license from ContraxSuite, LLC. Buying such a license is
    mandatory as soon as you develop commercial activities involving ContraxSuite
    software without disclosing the source code of your own applications.  These
    activities include: offering paid services to customers as an ASP or "cloud"
    provider, processing documents on the fly in a web application,
    or shipping ContraxSuite within a closed source product.
"""
# -*- coding: utf-8 -*-

# Standard imports
import builtins
import html
import inspect
import json
import re
from typing import List, Dict, Any, Optional

# Django imports
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected
from django.contrib.admin.options import TO_FIELD_VAR
from django.contrib.admin.utils import get_deleted_objects, unquote
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.postgres import fields
from django.contrib.postgres.forms.jsonb import JSONField, JSONString
from django.core.exceptions import FieldError, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render, redirect
from django.template.response import TemplateResponse
from django.urls import reverse, path
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _

# Third-party imports
import ast
from django_json_widget.widgets import JSONEditorWidget
from djangoql.admin import DjangoQLSearchMixin
from djangoql.exceptions import DjangoQLError
from djangoql.queryset import apply_search
from djangoql.schema import DjangoQLSchema, StrField
from simple_history.admin import SimpleHistoryAdmin
from pydash.strings import snake_case
from rest_framework.response import Response

# Project imports
from apps.common.collection_utils import chunks
from apps.common.expressions import PythonExpressionChecker
from apps.common.model_utils.model_class_dictionary import ModelClassDictionary
from apps.common.script_utils import ScriptError
from apps.common.url_utils import as_bool
from apps.common.utils import fetchone, full_reverse
from apps.common.widgets import EditableTableWidget
from apps.document import signals
from apps.document.constants import DOC_NUMBER_PER_MAIN_TASK
from apps.document.field_detection.detector_field_matcher import DetectorFieldMatcher
from apps.document.field_detection.field_based_ml_field_detection import init_classifier_impl
from apps.document.field_detection.formula_based_field_detection import \
    FormulaBasedFieldDetectionStrategy, DocumentFieldFormulaError
from apps.document.field_detection.stop_words import compile_stop_words, \
    detect_value_with_stop_words
from apps.document.field_processing.field_processing_utils import order_field_detection
from apps.document.field_types import RelatedInfoField, TypedField, ChoiceField
from apps.document.models import (Document, DocumentText, DocumentMetadata, DocumentField,
                                  DocumentType, FieldValue, FieldAnnotationStatus, FieldAnnotation,
                                  FieldAnnotationFalseMatch, FieldAnnotationSavedFilter,
                                  DocumentProperty, DocumentRelation, DocumentNote,
                                  DocumentFieldDetector, ExternalFieldValue, ClassifierModel,
                                  TextUnit, TextUnitProperty, TextUnitNote, TextUnitTag,
                                  TextUnitText, DocumentFieldCategory, DocumentFieldFamily,
                                  DocumentFieldMultilineRegexDetector)
from apps.document.python_coded_fields_registry import PYTHON_CODED_FIELDS_REGISTRY
from apps.document.repository.document_field_repository import DocumentFieldRepository
from apps.extract.models import Term
from apps.rawdb.constants import FIELD_CODE_ANNOTATION_SUFFIX, FIELD_CODE_HIDE_UNTIL_PYTHON, \
    FIELD_CODE_FORMULA
from apps.task.models import Task

__author__ = "ContraxSuite, LLC; LexPredict, LLC"
__copyright__ = "Copyright 2015-2020, ContraxSuite, LLC"
__license__ = "https://github.com/LexPredict/lexpredict-contraxsuite/blob/1.7.0/LICENSE"
__version__ = "1.7.0"
__maintainer__ = "LexPredict, LLC"
__email__ = "support@contraxsuite.com"


MLFLOW_DETECT_ON_DOCUMENT_LEVEL = 'mlflow_detect_on_document_level'

REQUIRES_TEXT_ANNOTATIONS = 'requires_text_annotations'

VALUE_DETECTION_STRATEGY = 'value_detection_strategy'


class ModelAdminWithPrettyJsonField(admin.ModelAdmin):
    """
    Mixin that prettifies JSON field representation
    """
    formfield_overrides = {
        fields.JSONField: {'widget': JSONEditorWidget},
    }


class PrettyJsonFieldMixin(object):
    """
    Mixin that prettifies JSON field representation
    """
    formfield_overrides = {
        fields.JSONField: {'widget': JSONEditorWidget},
    }


class DocumentTextInline(admin.TabularInline):
    model = DocumentText


class DocumentMetadatatInline(admin.TabularInline):
    model = DocumentMetadata


class TermUsageField(StrField):
    """
    Search by given number of full years
    """
    model = Document
    name = 'term_usage'

    def get_lookup_name(self):
        return 'term_usage'

    def get_lookup(self, path, operator, value):
        """
        The lookup should support with all operators compatible with IntField
        """
        if operator == 'in' or operator == 'not in':
            term_ids = list(Term.objects.filter(term__in=value).values_list('pk', flat=True))
        else:
            value = self.get_lookup_value(value)
            term_ids = list(Term.objects.filter(term=value).values_list('pk', flat=True))

        if operator == '=' or operator == 'in':
            return (Q(**{'termusage__term__id__in': term_ids}))
        elif operator == '!=' or operator == 'not in':
            return (~Q(**{'termusage__term__id__in': term_ids}))
        else:
            return (Q(**{}),)


class DocumentQLSchema(DjangoQLSchema):
    def get_fields(self, model):
        fields = super().get_fields(model)
        if model == Document:
            fields = [TermUsageField()] + fields
        return fields


def get_unique_search_results(admin_view: DjangoQLSearchMixin,
                              request, queryset, search_term):
    if admin_view.search_mode_toggle_enabled() and \
            not admin_view.djangoql_search_enabled(request):
        return super(DjangoQLSearchMixin, admin_view).get_search_results(
            request=request,
            queryset=queryset,
            search_term=search_term,
        )
    use_distinct = True
    if not search_term:
        return queryset, use_distinct
    try:
        return (
            apply_search(queryset, search_term, admin_view.djangoql_schema),
            use_distinct,
        )
    except (DjangoQLError, ValueError, FieldError, ValidationError) as e:
        msg = admin_view.djangoql_error_message(e)
        messages.add_message(request, messages.WARNING, msg)
        return queryset.none(), use_distinct


class DocumentAdmin(DjangoQLSearchMixin,
                    ModelAdminWithPrettyJsonField,
                    SimpleHistoryAdmin):
    djangoql_schema = DocumentQLSchema

    last_request = None

    list_display = ('name', 'doc_ref', 'document_type', 'project', 'status_name',
                    'source_type', 'paragraphs', 'sentences',)
    search_fields = ['document_type__code', 'name']
    advanced_filter_fields = [
        ('project__name', 'project',),
        ('status__name', 'status',),
        'paragraphs', 'sentences']
    inlines = [DocumentTextInline, DocumentMetadatatInline]

    change_list_template = 'admin/document/document/change_list.html'

    def get_queryset(self, request):
        self.last_request = request
        req_path = request.path.strip('/')
        if req_path.endswith('change'):
            self.djangoql_completion = False
        else:
            self.djangoql_completion = True

        return Document.all_objects

    def get_search_results(self, request, queryset, search_term):
        return get_unique_search_results(
            self, request, queryset, search_term)

    def doc_ref(self, obj):
        url = self.full_reverse('document:document-detail', args=[obj.pk])
        return mark_safe(f'<a href="{url}"><i class="fa fa-external-link"></i>{obj.pk}</a>')

    doc_ref.short_description = 'ID'

    def get_actions(self, request):
        actions = super().get_actions(request)
        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions

    @staticmethod
    def status_name(obj):
        return obj.status.name

    def has_delete_permission(self, request, obj=None):
        return False

    def full_reverse(self, *args, **kwargs):
        return full_reverse(*args, **kwargs, request=self.last_request)


class DocumentTextAdmin(ModelAdminWithPrettyJsonField, SimpleHistoryAdmin):
    search_fields = ['document__document_type__code', 'document__name']


class DocumentMetadataAdmin(ModelAdminWithPrettyJsonField, SimpleHistoryAdmin):
    search_fields = ['document__document_type__code', 'document__name']


class SoftDeleteDocument(Document):
    class Meta:
        proxy = True


class DeletePendingFilter(admin.SimpleListFilter):
    title = _('Status')

    parameter_name = 'delete_pending'

    def lookups(self, request, model_admin):
        return (
            ('deleting', _('Deleting')),
            ('all', _('All')),
        )

    def queryset(self, request, queryset):
        queryset = Document.all_objects
        val = self.value().lower() if self.value() else ''
        if val == 'all':
            return queryset
        return queryset.filter(delete_pending=True)


def set_soft_delete(document_ids: List[int], delete_not_undelete: bool, request):
    from apps.document.sync_tasks.soft_delete_document_task import SoftDeleteDocumentsSyncTask
    SoftDeleteDocumentsSyncTask().process(
        document_ids=document_ids,
        delete_not_undelete=delete_not_undelete)


def mark_deleting(_modeladmin, request, queryset):
    # queryset.update(delete_pending=True)
    document_ids = list(queryset.values_list('pk', flat=True))
    set_soft_delete(document_ids, True, request)


mark_deleting.short_description = "Mark selected documents for deleting"


def unmark_deleting(_, request, queryset):
    document_ids = list(queryset.values_list('pk', flat=True))
    set_soft_delete(document_ids, False, request)


unmark_deleting.short_description = "Uncheck selected documents for deleting"


def delete_checked_documents(_, request, queryset):
    ids = [d.pk for d in queryset]
    request.session['_doc_ids'] = ids
    return HttpResponseRedirect("./confirm_delete_view/")


delete_checked_documents.short_description = "Delete checked documents"


class SoftDeleteDocumentAdmin(DocumentAdmin):
    list_filter = [DeletePendingFilter]
    list_display = ('get_name', 'get_project', 'document_type', 'paragraphs', 'sentences', 'delete_pending')
    search_fields = ['name', 'project__name']
    actions = [mark_deleting, unmark_deleting, delete_checked_documents]
    change_list_template = 'admin/document/softdeletedocument/change_list.html'

    def get_project(self, obj):
        return obj.project.name if obj.project else ''

    get_project.short_description = 'Project'
    get_project.admin_order_field = 'project__name'

    def get_name(self, obj):
        display_text = "<a href={}>{}</a>".format(
            reverse('admin:{}_{}_change'.format(obj._meta.app_label,
                                                obj._meta.model_name),
                    args=(obj.pk,)),
            obj.name)
        return mark_safe(display_text)

    get_name.short_description = 'Document'
    get_name.admin_order_field = 'name'

    def get_actions(self, request):
        actions = super().get_actions(request)
        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def delete_all_checked(self, request):
        ids = [d.pk for d in Document.all_objects.filter(delete_pending=True)]
        request.session['_doc_ids'] = ids
        return HttpResponseRedirect("../confirm_delete_view/")

    def confirm_delete_view(self, request):
        from apps.document.repository.document_bulk_delete \
            import get_document_bulk_delete
        doc_ids = request.session.get('_doc_ids')

        if request.method == 'GET':
            details = request.GET.get('details') == 'true'

            del_count = []

            if details:
                items_by_table = get_document_bulk_delete().calculate_deleting_count(doc_ids)
                mdc = ModelClassDictionary()
                del_count_hash = {mdc.get_model_class_name_hr(t): items_by_table[t]
                                  for t in items_by_table if t in mdc.model_by_table}
                del_count = [(d, del_count_hash[d], False) for d in del_count_hash]
                del_count = sorted(del_count, key=lambda x: x[0])

            del_count.insert(0, ('Documents', len(doc_ids), True))

            context = {
                'deleting_count': del_count,
                'return_url': 'admin:document_softdeletedocument_changelist',
                'details': details
            }
            return render(request, "admin/document/softdeletedocument/confirm_delete_view.html", context)

        # POST: actual delete
        from apps.task.tasks import _call_task
        from apps.document.tasks import DeleteDocuments

        for doc_ids_chunk in chunks(doc_ids, DOC_NUMBER_PER_MAIN_TASK):
            _call_task(
                DeleteDocuments,
                _document_ids=doc_ids_chunk,
                user_id=request.user.id)

        self.message_user(request, "Started deleting for all checked documents")
        return HttpResponseRedirect(reverse('admin:document_softdeletedocument_changelist'))

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path('delete_all_checked/', self.delete_all_checked),
            path('confirm_delete_view/', self.confirm_delete_view),
        ]
        return my_urls + urls


PARAM_OVERRIDE_WARNINGS = 'override_warnings'


class UsersTasksValidationAdmin(admin.ModelAdmin):
    save_warning_template = 'admin/document/users_tasks_warning_save_form.html'
    delete_confirmation_template = 'admin/document/users_tasks_warning_delete_form.html'
    delete_selected_confirmation_template = 'admin/document/users_tasks_warning_delete_selected_form.html'

    class QSList(list):
        def __init__(self, qs):
            super().__init__(qs)
            self.qs = qs
            self.verbose_name = qs.model._meta.verbose_name
            self.verbose_name_plural = qs.model._meta.verbose_name_plural

        def count(self, **kwargs):
            return len(self)

        def delete(self):
            self.qs.delete()

    @classmethod
    def users_tasks_validation_enabled(cls):
        from apps.rawdb.app_vars import APP_VAR_DISABLE_RAW_DB_CACHING
        from apps.document.app_vars import ADMIN_RUNNING_TASKS_VALIDATION_ENABLED
        return not APP_VAR_DISABLE_RAW_DB_CACHING.val and ADMIN_RUNNING_TASKS_VALIDATION_ENABLED.val

    @classmethod
    def get_user_task_names(cls):
        if cls.users_tasks_validation_enabled():
            tasks = Task.objects \
                .get_active_user_tasks() \
                .distinct('name') \
                .order_by('name') \
                .values_list('name', flat=True)
            return list(tasks)
        else:
            return None

    WARN_CODE_TASKS_RUNNING = 'warning:tasks_running'

    @classmethod
    def validate_running_tasks(cls, request, dst_errors_dict: Dict[str, Any]):
        if not cls.users_tasks_validation_enabled():
            return

        if not as_bool(request.GET, PARAM_OVERRIDE_WARNINGS):
            user_tasks = UsersTasksValidationAdmin.get_user_task_names()
            if user_tasks:
                user_tasks = '; <br />'.join(user_tasks)
                dst_errors_dict[cls.WARN_CODE_TASKS_RUNNING] = f'''The following background tasks are running at 
                the current moment:\n<br />{user_tasks}.\n<br />
                The Save operation can cause their crashing because of the document type / field structure changes.'''

    @staticmethod
    def _get_deleted_objects(objects, request, admin_site=None):
        return get_deleted_objects(objects, request, admin_site)

    def delete_selected_action(self, model_admin, request, qs):
        objects = UsersTasksValidationAdmin.QSList(qs)
        if request.POST.get('post'):
            deletable_objects, model_count, perms_needed, protected = self._get_deleted_objects(
                objects, request, model_admin.admin_site)
            if not protected:
                res = delete_selected(model_admin, request, objects)
                for obj in objects:
                    self.on_object_deleted(obj)
                return res
        model_admin.get_deleted_objects = self._get_deleted_objects
        response = delete_selected(model_admin, request, objects)
        context = response.context_data
        context['running_tasks'] = self.get_user_task_names() or None
        return TemplateResponse(request, model_admin.delete_selected_confirmation_template, context)

    def get_actions(self, request):
        actions = super().get_actions(request)
        func, name, title = actions['delete_selected']
        actions['delete_selected'] = (self.delete_selected_action, name, title)
        return actions

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        self.on_object_deleted(obj)

    def on_object_deleted(self, obj):
        raise RuntimeError('Not implemented')

    @classmethod
    def _get_confirm_action_name(cls, base_action):
        return '_confirm' + base_action

    def build_warning_context(self, object_id, form) -> dict:
        warning_context = {}
        user_tasks = self.get_user_task_names()
        if user_tasks:
            warning_context['running_tasks'] = user_tasks
        return warning_context

    def _prepare_form_template(self, request, object_id, extra_context=None):
        self.change_form_template = None
        if request.method == 'POST':
            base_actions = ['_save', '_continue', '_addanother']
            source_action = None
            for action in base_actions:
                if self._get_confirm_action_name(action) in request.POST:
                    request.POST = request.POST.copy()
                    request.POST[action] = request.POST[self._get_confirm_action_name(action)]
                    source_action = action
                    break
            if not source_action:
                form = None
                to_field = request.POST.get(TO_FIELD_VAR, request.GET.get(TO_FIELD_VAR))
                if not to_field or (to_field and self.to_field_allowed(request, to_field)):
                    obj = self.get_object(request, unquote(object_id), to_field) if object_id else None
                    ModelForm = self.get_form(request, obj)
                    form = ModelForm(request.POST, request.FILES, instance=obj)
                if not form or not form.is_valid():
                    return extra_context
                warning_context = self.build_warning_context(object_id, form)
                if warning_context:
                    for action in base_actions:
                        if action in request.POST:
                            extra_context = {**warning_context, **extra_context} if extra_context else warning_context
                            self.change_form_template = self.save_warning_template
                            extra_context['confirmation_button_name'] = self._get_confirm_action_name(action)
                            extra_context['source_action'] = action
                            break
        return extra_context

    def change_view(self, request, object_id, form_url='', extra_context=None):
        with transaction.atomic():
            extra_context = self._prepare_form_template(request, object_id, extra_context)
            return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def render_delete_form(self, request, context):
        context = context or {}
        context['running_tasks'] = self.get_user_task_names() or None
        return super().render_delete_form(request, context)

    @classmethod
    def get_confirmation_form(cls, form_class):

        class ConfirmationForm(form_class):
            def is_valid(self):
                return False

        return ConfirmationForm

    def get_form(self, request, obj=None, **kwargs):
        form_class = super().get_form(request, obj=None, **kwargs)
        if self.change_form_template == self.save_warning_template:
            form_class = self.get_confirmation_form(form_class)
        return form_class


class FieldValuesValidationAdmin(UsersTasksValidationAdmin):
    save_warning_template = 'admin/document/field_values_warning_save_form.html'

    def build_warning_context(self, object_id, form) -> dict:
        import apps.document.repository.document_field_repository as dfr
        field_repo = dfr.DocumentFieldRepository()
        warning_context = super().build_warning_context(object_id, form)
        document_type_changed = 'document_type' in form.changed_data
        field_type_changed = 'type' in form.changed_data
        if object_id is not None and self.model is DocumentField \
                and (document_type_changed or field_type_changed):
            values_number = field_repo.get_doc_field_values_filtered_count(
                form.instance.pk)
            if values_number > 0:
                user_values_number = field_repo.get_filtered_field_values_count(
                    form.instance.pk)
                if values_number > 0:
                    detected_values_number = values_number - user_values_number
                    detected_values_number = detected_values_number if detected_values_number > 0 else 0
                    warning_context['values_validation_user_values_number'] = user_values_number
                    warning_context['values_validation_detected_values_number'] = detected_values_number
                    modified_fields = None
                    if document_type_changed and field_type_changed:
                        modified_fields = 'document type and field type'
                    elif document_type_changed:
                        modified_fields = 'document type'
                    elif field_type_changed:
                        modified_fields = 'field type'
                    warning_context['values_validation_modified_fields'] = modified_fields
        return warning_context

    def add_view(self, request, form_url='', extra_context=None):
        with transaction.atomic():
            extra_context = self._prepare_form_template(request, None, extra_context)
            return super().add_view(request, form_url, extra_context)


class DocumentFieldFormulaCheck:

    def __init__(self, request_data, document_field=None, return_json_response=True):

        self.argument_errors = []
        self.dependent_field_ids = []
        self.document_field = document_field
        self.check_return_value = True
        self.return_json_response = return_json_response

        self.field_type = request_data.get('field_type') or \
                          (self.document_field.type if self.document_field else None)
        if not self.field_type:
            self.argument_errors.append('"field_type" was not provided')

        self.document_type_id = request_data.get('document_type') or \
                                (self.document_field.document_type.pk if self.document_field else None)
        if not self.document_type_id:
            self.argument_errors.append('"document_type.pk" was not provided')

        # if "hide_until_python"
        if FIELD_CODE_HIDE_UNTIL_PYTHON in request_data:
            self.dependent_field_ids = DocumentField.objects.filter(
                document_type=self.document_type_id).values_list('uid', flat=True)
        # React UI
        elif 'depends_on_fields' in request_data:
            self.dependent_field_ids = request_data['depends_on_fields']
        # Django admin site
        elif 'depends_on_fields[]' in request_data:
            self.dependent_field_ids = request_data.getlist('depends_on_fields[]')
        # this shouldn't be a case
        else:
            # if no depends_on_fields passed try to use existing
            if document_field is not None:
                self.dependent_field_ids = document_field.depends_on_fields.values_list('uid', flat=True)
            # otherwise if it's a NEW field
            else:
                self.dependent_field_ids = DocumentField.objects.filter(
                    document_type=self.document_type_id).values_list('uid', flat=True)

        if FIELD_CODE_FORMULA in request_data:
            self.formula = request_data.get(FIELD_CODE_FORMULA)
        elif FIELD_CODE_HIDE_UNTIL_PYTHON in request_data:
            self.formula = request_data.get(FIELD_CODE_HIDE_UNTIL_PYTHON)
            self.check_return_value = False
        else:
            self.argument_errors.append('either "formula" or "hide_until_python" '
                                        'should be provided in a request')

        if document_field:
            try:
                _ = TypedField.by(document_field)
            except KeyError:
                raise RuntimeError(f'No TypedField obtained from {document_field.code}')

    def check(self):

        # TODO: simplify the methods below
        if self.argument_errors:
            data = {'errors': self.argument_errors}
        else:
            rst_default = self.get_formula_errors(check_on_empty_values=False)
            if rst_default.calculated:
                rst_empty = self.get_formula_errors(check_on_empty_values=True)
                if rst_empty.errors:
                    rst_empty.errors = [i for i in rst_empty.errors if i not in rst_default.errors]
                    if rst_empty.errors:
                        rst_default.warnings += ['There were errors with null-initialized fields:']
                        rst_default.warnings += rst_empty.errors

            if rst_default.errors:
                default_error_message = ['Error in calculating formula. Revise formula and try again.']
                rst_default.errors = default_error_message + rst_default.errors
            data = rst_default.__dict__

        if self.return_json_response:
            return Response(data, content_type='application/json')
        return data

    def get_formula_errors(self, check_on_empty_values: bool):
        dependent_fields = list(DocumentField.objects.filter(uid__in=self.dependent_field_ids))

        if check_on_empty_values:
            fields_to_values = {field.code: None for field in dependent_fields}
        else:
            fields_to_values = {field.code: TypedField.by(field).example_python_value()
                                for field in dependent_fields}

        if not self.document_field:
            document_field = DocumentField()
            document_field.type = self.field_type
        else:
            document_field = self.document_field
        result = self.calculate_formula_result_on_values(document_field, fields_to_values)
        return result

    def calculate_formula_result_on_values(self,
                                           document_field: DocumentField,
                                           fields_to_values: Dict[str, Any]):
        result = self.FormulaCheckResult(calculated=False)

        # TODO: duplicate?
        try:
            typed_field = TypedField.by(document_field)
        except KeyError:
            result.errors.append(f'Unknown field type "{document_field.type}".')
            return result

        try:
            result.value = FormulaBasedFieldDetectionStrategy.calc_formula(
                document_field.code, self.formula, fields_to_values)
            result.calculated = True
        except Exception as e:
            msg = str(e.base_error) if hasattr(e, 'base_error') and \
                document_field.code == FIELD_CODE_HIDE_UNTIL_PYTHON else str(e)
            result.errors.append(msg)

        if result.calculated:
            checker = PythonExpressionChecker(self.formula)
            checker.test_expression()
            if checker.warnings:
                result.warnings += checker.warnings
            name_error = self.check_formula_refs(self.formula, document_field, fields_to_values)
            if name_error:
                result.errors += [name_error]
        if self.check_return_value:
            if result.calculated and not typed_field.is_python_field_value_ok(result.value):
                result.errors.append(f'Formula returned value not suitable for this field:\n{result.value}')
        return result

    @staticmethod
    def check_formula_refs(formula: str,
                           field: DocumentField,
                           fields_to_values: Dict[str, Any]) -> Optional[str]:
        try:
            st = ast.parse(formula)
        except Exception as e:
            return f'Error parsing formula: {e}'
        var_names = []
        reserved_words = dir(builtins) + list(settings.SCRIPTS_BASE_EVAL_LOCALS.keys())
        for node in ast.walk(st):
            if isinstance(node, ast.Name) and node.id not in reserved_words:
                var_names.append(node.id)
        # get field deps
        deps = {v for v in fields_to_values}
        if field.depends_on_fields.exists():
            field_deps = {f for f in field.depends_on_fields.values_list('code', flat=True)}
            deps.update(field_deps)
        missing_fields = [n for n in var_names if n not in deps]
        if not missing_fields:
            return None
        return 'Formula references ' + ', '.join([f'"{v}"' for v in missing_fields]) + \
               ' that are not found in field dependencies (' + \
               ', '.join([f'"{v}"' for v in deps]) + ')'

    class FormulaCheckResult:
        def __init__(self, calculated: bool, value: Any = None, errors: List[str] = None,
                     warnings: List[str] = None):
            self.calculated = calculated
            self.value = value
            self.errors = errors or []
            self.warnings = warnings or []

        def to_json(self):
            return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)


class ModelFormWithUnchangeableFields(forms.ModelForm):
    """
    Mixin provides ability to freeze some fields of objects via form_field.disabled attribute -
    i.e. it doesn't allow to modify fields declared in iterable UNCHANGEABLE_FIELDS.
    Note that db object and form field should have the same name to make it working.
    """
    UNCHANGEABLE_FIELDS = ()

    @property
    def is_update(self):
        """
        Usually this check looks like `if self.instance.pk is not None` but
        instances with uid instead of id initially has pk
        """
        return self.Meta.model.objects.filter(pk=self.instance.pk).exists()

    def __init__(self, *args, **kwargs):
        """
        Set "form_field.disabled" attribute from UNCHANGEABLE_FIELDS
        """
        super().__init__(*args, **kwargs)
        if self.is_update:
            for field_name in self.UNCHANGEABLE_FIELDS:
                if field_name in self.fields:
                    self.fields[field_name].disabled = True
        for field_name in getattr(self.Meta, 'readonly_fields', []):
            if field_name in self.fields:
                self.fields[field_name].disabled = True
        for field in self.fields.values():
            if field.disabled and not isinstance(field.widget, forms.widgets.CheckboxInput):
                widget = field.widget
                if hasattr(widget, 'attrs'):
                    widget.attrs['readonly'] = True
                if hasattr(widget, 'can_add_related'):
                    widget.can_add_related = False
                if hasattr(widget, 'can_delete_related'):
                    widget.can_delete_related = False


class DocumentFieldForm(ModelFormWithUnchangeableFields):
    MAX_ESCAPED_FIELD_CODE_LEN = 50
    R_FIELD_CODE = re.compile(r'^[a-z][a-z0-9_]*$')
    UNCHANGEABLE_FIELDS = ('code', 'long_code', 'document_type', 'type', 'hide_until_js')

    class DefaultValueField(JSONField):
        def to_python(self, value):
            try:
                return super().to_python(value)
            except forms.ValidationError as e:
                if isinstance(value, str):
                    return JSONString(value)
                raise e

    default_value = DefaultValueField(required=False)

    depends_on_fields = forms.ModelMultipleChoiceField(
        queryset=DocumentField.objects.all(),
        required=False,
        widget=FilteredSelectMultiple('depends_on_fields', False))

    hide_until_python = forms.CharField(
        widget=forms.Textarea,
        required=False,
        help_text="""        
            Enter a boolean expression in Python syntax. If this Python expression evaluates to True, then this 
            Document Field will be displayed in the user interface. Likewise, if this Python expression evaluates to 
            False, then this Document Field will be hidden from view. Importantly, if a document’s status is set to 
            complete and this Document Field remains hidden, then this Document Field’s data will be erased. Similarly, 
            this Document Field might contain data that a user can not review if it is hidden and the document has not 
            been set to complete."""
    )

    hide_until_js = forms.CharField(
        widget=forms.Textarea,
        required=False,
        disabled=True,
        help_text='Target expression ("Hide until python" expression converted to JavaScript syntax for frontend). '
                  'Allowed operators: +, -, *, /, ===, !==, ==, !=, &&, ||, >, <, >=, <=, %')

    long_code = forms.CharField(
        required=False,
        disabled=True)

    classifier_init_script = forms.Field(
        required=False, widget=forms.Textarea,
        help_text=mark_safe(
            'Classifier initialization script. '
            'Here is how it used: <br /><br />' + '<br />'.join(
                inspect.getsource(init_classifier_impl)
                    .replace(' ', '&nbsp;')
                    .replace('\t', '&nbsp;&nbsp;&nbsp;&nbsp;')
                    .split('\n'))))

    class Meta:
        model = DocumentField
        fields = '__all__'
        readonly_fields = ('created_by', 'modified_by')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['type'].required = True
        self.fields['order'].required = False
        self.fields['text_unit_type'].required = False
        self.fields['value_detection_strategy'].required = False
        self.fields['trained_after_documents_number'].required = False
        self.fields['detect_limit_count'].required = True
        self.fields['display_yes_no'].required = False

    @classmethod
    def _extract_field_and_deps(cls, base_fields: List[DocumentField], fields_buffer: dict) -> dict:
        for field in base_fields:
            if field.code not in fields_buffer:
                fields_buffer[field.code] = set(field.get_depends_on_codes()) or set()
                cls._extract_field_and_deps(field.depends_on_fields.all(), fields_buffer)
        return fields_buffer

    def calc_formula(self, formula_value, formula_field_name) -> Any:
        if self.is_valid():
            data = dict(
                field_type=self.cleaned_data['type'],
                document_type=self.cleaned_data['document_type'].pk,
                depends_on_fields=self.cleaned_data['depends_on_fields'].values_list('uid', flat=True)
            )
            data[snake_case(formula_field_name)] = formula_value
            res = DocumentFieldFormulaCheck(data, document_field=self.instance, return_json_response=False).check()
            if res.get('errors'):
                self.add_error(formula_field_name, '\n '.join(res['errors']))

    PARAM_CODE = 'code'

    def validate_field_code(self):

        field_code = self.cleaned_data.get(self.PARAM_CODE) or ''

        if not self.R_FIELD_CODE.match(field_code):
            self.add_error('code', '''Field codes must be lowercase, should start with a latin letter and contain 
            only latin letters, digits, and underscores. You cannot use a field code you have already used for this 
            document type.''')

        reserved_suffixes = ('_sug', '_txt', FIELD_CODE_ANNOTATION_SUFFIX)
        # TODO: define reserved suffixes/names in field_value_tables.py? collect/autodetect?
        for suffix in reserved_suffixes:
            if field_code.endswith(suffix):
                self.add_error('code', '''"{}" suffix is reserved.
                 You cannot use a field code which ends with this suffix.'''.format(suffix))

    def clean_detect_limit_count(self):
        detect_limit_count = self.cleaned_data['detect_limit_count']
        detect_limit_unit = self.cleaned_data['detect_limit_unit']
        if detect_limit_unit == DocumentField.DETECT_LIMIT_NONE:
            return 0
        if detect_limit_unit != DocumentField.DETECT_LIMIT_NONE and detect_limit_count == 0:
            self.add_error('detect_limit_count', '"Detect Limit Count" must be greater than "0".')
        return detect_limit_count

    UNSURE_CHOICE_VALUE = 'unsure_choice_value'
    UNSURE_THRESHOLDS = 'unsure_thresholds_by_value'

    def _post_clean(self):
        super()._post_clean()
        # At this stage self.instance is filled with the data from self.cleaned_data but still not saved.
        # Here we can apply validation on the self.instance.
        document_field: DocumentField = self.instance
        formula = document_field.formula
        hide_until_python = document_field.hide_until_python
        type_code = document_field.type
        default_value = document_field.default_value

        typed_field = None
        try:
            typed_field = TypedField.by(self.instance)
        except KeyError:
            self.add_error('type', 'Unknown field type "{}".'.format(type_code))

        if formula and formula.strip() and type_code:
            self.calc_formula(formula, 'formula')

        hide_until_python = hide_until_python.strip() if hide_until_python else None
        if hide_until_python:
            self.calc_formula(hide_until_python, 'hide_until_python')

        if default_value is not None:
            if type_code == RelatedInfoField.type_code:
                self.add_error('default_value', 'Related info field can\'t have default value')
            elif typed_field.extract_from_possible_value(default_value) != default_value:
                self.add_error('default_value', 'Wrong value for type {0}. Example: {1}'
                               .format(type_code, json.dumps(typed_field.example_python_value())))

        value_detection_strategy = self.cleaned_data[VALUE_DETECTION_STRATEGY]
        from apps.document.field_detection.field_detection import FIELD_DETECTION_STRATEGY_REGISTRY
        if value_detection_strategy not in FIELD_DETECTION_STRATEGY_REGISTRY:
            self.add_error(VALUE_DETECTION_STRATEGY, f'Unknown value detection strategy {value_detection_strategy}')
        strategy_problems = FIELD_DETECTION_STRATEGY_REGISTRY[value_detection_strategy] \
            .has_problems_with_field(self.instance)
        if strategy_problems:
            self.add_error(VALUE_DETECTION_STRATEGY, strategy_problems)

    def clean(self):
        # At this stage self.instance is not filled with the values from the form.
        # All validation should be applied on the form entries passed in self.cleaned_data.
        field_code = self.cleaned_data.get('code')
        type_code = self.cleaned_data.get('type')
        depends_on_fields = self.cleaned_data.get('depends_on_fields') or []
        depends_on_fields = list(depends_on_fields)
        classifier_init_script = self.cleaned_data['classifier_init_script']
        stop_words = self.cleaned_data.get('stop_words')
        unsure_choice_value = self.cleaned_data[self.UNSURE_CHOICE_VALUE]
        choice_values = DocumentField.parse_choice_values(self.cleaned_data['choices'])
        unsure_thresholds_by_value = self.cleaned_data.get(self.UNSURE_THRESHOLDS)
        requires_text_annotations = self.cleaned_data.get(REQUIRES_TEXT_ANNOTATIONS)
        mlflow_detect_on_document_level = self.cleaned_data.get(MLFLOW_DETECT_ON_DOCUMENT_LEVEL)
        category = self.cleaned_data.get('category')
        document_type = self.cleaned_data.get('document_type')

        if category and category.document_type != document_type:
            self.add_error('category',
                           'Category must have the same Document Type selected above.')

        if requires_text_annotations and mlflow_detect_on_document_level:
            self.add_error(MLFLOW_DETECT_ON_DOCUMENT_LEVEL, 'The field requires text annotations, detection '
                                                            'can be done on text unit level only.')
        if choice_values:
            choice_errors = ChoiceField.check_choice_values_list(choice_values)
            for ch_err in choice_errors:
                self.add_error('choices', ch_err)

        try:
            DocumentField.compile_value_regexp(self.cleaned_data['value_regexp'])
        except Exception as exc:
            self.add_error('value_regexp', exc)

        self.validate_field_code()

        if unsure_choice_value and (not choice_values or unsure_choice_value not in choice_values):
            self.add_error(self.UNSURE_CHOICE_VALUE, '"Unsure choice value" must be listed in the choice values.')

        if unsure_thresholds_by_value is not None:
            if not hasattr(unsure_thresholds_by_value, 'items'):
                self.add_error(self.UNSURE_THRESHOLDS, 'Must be a dict of choice values to float thresholds [0..1]')
            else:
                if not choice_values:
                    self.add_error(self.UNSURE_THRESHOLDS, '"Unsure" thresholds are set but choice values are not.')
                if not unsure_choice_value:
                    self.add_error(self.UNSURE_THRESHOLDS, '"Unsure" thresholds are set but '
                                                           '"unsure" choice value is not.')

                if choice_values and unsure_choice_value:
                    for k, v in unsure_thresholds_by_value.items():
                        if k == unsure_choice_value:
                            self.add_error(self.UNSURE_THRESHOLDS, 'Please set thresholds only for "sure" choice '
                                                                   'values and not for ' + k)
                        elif k not in choice_values:
                            self.add_error(self.UNSURE_THRESHOLDS, 'Value not in choice values: ' + k)
                        if (not isinstance(v, int) and not isinstance(v, float)) or v < 0 or v > 1:
                            self.add_error(self.UNSURE_THRESHOLDS,
                                           'Threshold should be a float value between 0 and 1: ' + k)

        try:
            stop_words = compile_stop_words(stop_words)
            detect_value_with_stop_words(stop_words, 'dummy text')
        except Exception as err:
            self.add_error('stop_words', str(err))

        try:
            init_classifier_impl(field_code, classifier_init_script)
        except ScriptError as err:
            self.add_error('classifier_init_script', str(err).split('\n'))

        fields_and_deps = {self.cleaned_data.get('code') or 'xxx': {f.code for f in depends_on_fields}}
        fields_and_deps = self._extract_field_and_deps(depends_on_fields, fields_and_deps)
        fields_and_deps = [(code, deps) for code, deps in fields_and_deps.items()]
        try:
            order_field_detection(fields_and_deps)
        except ValueError as ve:
            self.add_error(None, str(ve))

        python_coded_field_code = self.cleaned_data.get('python_coded_field')
        if python_coded_field_code:
            python_coded_field = PYTHON_CODED_FIELDS_REGISTRY.get(python_coded_field_code)
            if not python_coded_field:
                self.add_error('python_coded_field', 'Unknown Python-coded field: {0}'.format(python_coded_field_code))
            else:
                if type_code != python_coded_field.type:
                    self.add_error('type', 'Python-coded field {0} is of type {1} but {2} is specified'
                                           ' as the field type'.format(python_coded_field.title,
                                                                       python_coded_field.type,
                                                                       type_code))

        if self.initial and 'type' in self.changed_data:
            wrong_field_detector_pks = []
            for field_detector in DocumentFieldDetector.objects.filter(field=self.instance):
                try:
                    DetectorFieldMatcher.validate_detected_value(type_code, field_detector.detected_value)
                except Exception:
                    wrong_field_detector_pks.append('#' + field_detector.pk)
            if wrong_field_detector_pks:
                self.add_error('type', 'Detected value is not allowed for this field type, please unset detected value '
                                       'for this field detectors: {0}'.format(', '.join(wrong_field_detector_pks)))

        return self.cleaned_data


class DocumentFieldAdmin(FieldValuesValidationAdmin):
    change_form_template = 'admin/document/documentfield/change_form.html'
    delete_confirmation_template = 'admin/document/documentfield/delete_confirmation.html'
    delete_selected_confirmation_template = 'admin/document/documentfield/delete_selected_confirmation.html'
    form = DocumentFieldForm

    list_display = (
        'document_type', 'code', 'category', 'order', 'title', 'description', 'type', 'formula', 'value_regexp', 'user',
        'modified_date', 'confidence')
    search_fields = ['document_type__code', 'code', 'category__name', 'title', 'description', 'created_by__username',
                     'confidence']
    filter_horizontal = ('depends_on_fields',)

    fieldsets = [
        ('General', {
            'fields': (
                'created_by', 'modified_by', 'code', 'long_code',
                'title', 'type', 'document_type', 'category', 'family', 'order', 'description',
                'confidence', 'requires_text_annotations', 'read_only', 'default_value'),
        }),
        ('Choice / Multi-choice Fields', {
            'fields': ('choices', 'allow_values_not_specified_in_choices'),
        }),
        ('Frontend Options', {
            'fields': ('hidden_always', 'hide_until_python', 'hide_until_js', 'display_yes_no',),
        }),
        ('Field Detection: General', {
            'fields': ('value_detection_strategy', 'text_unit_type', 'depends_on_fields',
                       'detect_limit_unit', 'detect_limit_count'),
        }),
        ('Field Detection: Regexp-based', {
            'fields': ('stop_words', 'value_regexp'),
        }),
        ('Field Detection: Machine Learning', {
            'fields': ('classifier_init_script', 'unsure_choice_value', 'unsure_thresholds_by_value',
                       'training_finished', 'dirty', 'trained_after_documents_number', 'vectorizer_stop_words'),
        }),
        ('Field Detection: Calculated Fields', {
            'fields': ('formula',),
        }),
        ('Field Detection: Python-coded Fields', {
            'fields': ('python_coded_field',),
        }),
        ('Field Detection: MLFlow', {
            'fields': ('mlflow_model_uri', 'mlflow_detect_on_document_level'),
        }),
        ('Metadata', {
            'fields': ('metadata',),
        }),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = 'docfield_admin'
        self.is_clone_view = False

    def get_search_results(self, request, queryset, search_term):
        qs, has_duplicates = super().get_search_results(request, queryset, search_term)
        return qs.select_related('document_type', 'modified_by'), has_duplicates

    def save_model(self, request, obj: DocumentField, form, change: bool):
        if self.is_clone_view is True:
            return

        typed = TypedField.by(obj)
        if change and (typed.is_choice_field and not obj.allow_values_not_specified_in_choices):
            # don't save here, prompt user if there are values other than in choices list
            return
        # check formula errors
        errors = {}  # type:Dict[str, List[str]]
        warnings = {}  # type:Dict[str, List[str]]

        formula_fields = ['formula']
        for field in formula_fields:
            rst_formula = DocumentFieldFormulaCheck(
                form.cleaned_data, form.instance, return_json_response=False).check()
            errors[field] = rst_formula['errors']
            warnings[field] = rst_formula['warnings']

        if errors:
            for field in errors:
                if not errors[field]:
                    continue
                msg = f'There were errors with field "{field}":\n\n'
                msg += '\n\n'.join(errors[field])
                messages.add_message(
                    request, messages.ERROR, msg)
        if warnings:
            for field in warnings:
                if not warnings[field]:
                    continue
                msg = f'There were potential issues with field "{field}":\n\n'
                msg += '\n\n'.join(warnings[field])
                messages.add_message(
                    request, messages.WARNING, msg)

        super().save_model(request, obj, form, change)
        signals.document_field_changed.send(self.__class__, user=request.user, document_field=obj)

    def response_change(self, request, obj: DocumentField):
        if self.is_clone_view:
            return self.response_clone(request, obj)
        return self.process_add_or_change('CHANGE', request, obj)

    def response_add(self, request, obj: DocumentField,
                     post_url_continue=None):
        return self.process_add_or_change('ADD', request, obj, post_url_continue)

    def process_add_or_change(self,
                              action: str,  # 'ADD' or 'CHANGE'
                              request, obj: DocumentField,
                              post_url_continue=None):
        typed = TypedField.by(obj)
        if not typed.is_choice_field or obj.allow_values_not_specified_in_choices:
            # object is already saved, continue
            if action == 'ADD':
                return super(DocumentFieldAdmin, self).response_add(
                    request, obj, post_url_continue)
            # if action == 'CHANGE':
            return super(DocumentFieldAdmin, self).response_change(request, obj)

        return self.confirm_newchoices_view(request, obj=obj)

    def confirm_newchoices_view(self, request, **kwargs):

        # if new object is directly passed to the function
        if 'obj' in kwargs:
            new_field = kwargs.get('obj')
        # if view is called via URL - get object by object_id passed in url
        elif 'object_id' in kwargs:
            new_field = self.get_object(request, kwargs['object_id'])
        # otherwise fail
        else:
            raise RuntimeError('Failed to get field object.')

        repo = DocumentFieldRepository()
        has_more, wrong_values = repo.get_wrong_choice_options(new_field)
        return_url = reverse('admin:document_documentfield_change',
                             kwargs={'object_id': new_field.pk})

        if not wrong_values:
            # just save the object
            new_field.save()
            signals.document_field_changed.send(
                self.__class__, user=request.user, document_field=new_field)
            messages.add_message(
                request, messages.INFO,
                f'The document field "{new_field.document_type}: {new_field.code}" was changed successfully.')

            # in case if "SAVE" button is used "Save and Continue"
            if '_save' in request.POST:
                return redirect('admin:document_documentfield_changelist')
            # "Save and Continue"
            else:
                return redirect(return_url)  # request.path?

        context = {
            'field_name': new_field.title,
            'field_code': new_field.code,
            'has_more': has_more,
            'wrong_values': wrong_values,
            'return_url': mark_safe(return_url)
        }
        return render(request, "admin/document/documentfield/confirm_newchoices_view.html", context)

    def on_object_deleted(self, obj):
        signals.document_field_deleted.send(self.__class__, user=None, document_field=obj)

    @staticmethod
    def user(obj):
        return obj.modified_by.username if obj.modified_by else None

    def change_view(self, request, object_id, form_url='', extra_context=None, **kwargs):
        extra_context = extra_context or dict()
        extra_context.update(kwargs)
        self.is_clone_view = 'clone' in kwargs
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def response_clone(self, request, obj):
        opts = self.model._meta
        msg_dict = {
            'name': opts.verbose_name,
            'obj': format_html('<a href="{}">{}</a>', reverse('admin:document_documentfield_change', args=[obj.pk]), obj),
        }
        msg = format_html(
            _('The {name} "{obj}" was cloned successfully.'),
            **msg_dict
        )
        self.message_user(request, msg, messages.SUCCESS)
        return self.response_post_save_change(request, obj)

    def save_form(self, request, form, change):
        if self.is_clone_view:
            from apps.document.api.v1 import DocumentFieldViewSet
            target_document_type = form.cleaned_data['document_type']
            new_field = DocumentFieldViewSet.clone_field(
                source_field=form.instance,
                target_document_type=target_document_type,
                new_field_code=form.cleaned_data['code'])
            return new_field
        return super().save_form(request, form, change)

    def get_form(self, request, obj=None, **kwargs):
        if self.is_clone_view:
            from apps.document.forms import CloneDocumentFieldForm
            return CloneDocumentFieldForm
        return super().get_form(request, obj=None, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if self.is_clone_view:
            fieldsets = [
                (f'Clone Document Field: {obj}', {'fields': ('code', 'document_type')})
            ]
            return fieldsets
        return self.fieldsets

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path('confirm_newchoices_view/<str:object_id>/',
                 self.confirm_newchoices_view,
                 name='confirm_newchoices_view'),
            path('<str:object_id>/clone/',
                 self.change_view,
                 kwargs={'clone': True},
                 name='clone_document_field')]
        return my_urls + urls

    @staticmethod
    def _get_deleted_objects(objects, request, admin_site=None):
        """
        Custom method to display related objects
        Otherwise default collector method works too long and throws 504 on FE side
        """
        from apps.project.models import Project
        if isinstance(objects, list):
            doc_types = [i.document_type for i in objects]
        elif isinstance(objects, UsersTasksValidationAdmin.QSList):
            doc_types = objects.qs.values_list('document_type')
        else:
            raise NotImplementedError()
        model_count = dict(
            related_projects=Project.all_objects.filter(type__in=doc_types).count(),
            related_projects_delete_pending=Project.all_objects.filter(type__in=doc_types, delete_pending=True).count(),
            related_documents=Document.all_objects.filter(document_type__in=doc_types).count(),
            related_documents_delete_pending=Document.all_objects.filter(document_type__in=doc_types, delete_pending=True).count(),
            document_field_detectors=DocumentFieldDetector.objects.filter(field__in=objects).count()
        )
        return [], model_count, [], []

    def get_deleted_objects(self, objs, request):
        return self._get_deleted_objects(objs, request, self.admin_site)

    def _delete_view(self, request, object_id, extra_context):
        # patch 'model_count' context to get dict in context instead of dict-items
        response = super()._delete_view(request, object_id, extra_context)
        if response and hasattr(response, 'context_data'):
            context = response.context_data
            context['model_count'] = dict(context['model_count'])
        return response

    def delete_selected_action(self, model_admin, request, qs):
        # patch 'model_count' context to get dict in context instead of dict-items
        response = super().delete_selected_action(model_admin, request, qs)
        if response and hasattr(response, 'context_data'):
            context = response.context_data
            context['model_count'] = dict(context['model_count'])
        return response


class DocumentTypeListFilter(admin.SimpleListFilter):
    # Human-readable title which will be displayed in the
    # right admin sidebar just above the filter options.
    title = 'Document'

    # Parameter for the filter that will be used in the URL query.
    parameter_name = 'field__document_type__code'

    default_value = 'All'

    def lookups(self, request, model_admin):
        """
        Returns a list of tuples. The first element in each
        tuple is the coded value for the option that will
        appear in the URL query. The second element is the
        human-readable name for the option that will appear
        in the right sidebar.
        """
        cats = set([v for v in DocumentFieldDetector.objects.all().values_list('field__document_type__code', flat=True)])
        return sorted([(c or '', c or '') for c in cats], key=lambda c: c[0])
        # return [('Common', 'Common'), ('Document', 'Document'), ('Extract', 'Extract')]

    def queryset(self, request, queryset):
        """
        Returns the filtered queryset based on the value
        provided in the query string and retrievable via
        `self.value()`.
        """
        if not self.value() or self.value() == 'All':
            return queryset
        return queryset.filter(field__document_type__code=self.value())


class FieldDetectorListFilter(admin.SimpleListFilter):
    # Human-readable title which will be displayed in the
    # right admin sidebar just above the filter options.
    title = 'Field'

    # Parameter for the filter that will be used in the URL query.
    parameter_name = 'field__title'

    default_value = 'All'

    def lookups(self, request, model_admin):
        """
        Returns a list of tuples. The first element in each
        tuple is the coded value for the option that will
        appear in the URL query. The second element is the
        human-readable name for the option that will appear
        in the right sidebar.
        """
        detector_query = DocumentFieldDetector.objects.all()
        if 'field__document_type__code' in request.GET:
            doc_code = request.GET['field__document_type__code']
            if doc_code:
                detector_query = DocumentFieldDetector.objects.filter(field__document_type__code=doc_code)
        cats = set([v for v in detector_query.values_list('field__title', flat=True)])
        return sorted([(c, c) for c in cats], key=lambda c: c[0])
        # return [('Common', 'Common'), ('Document', 'Document'), ('Extract', 'Extract')]

    def queryset(self, request, queryset):
        """
        Returns the filtered queryset based on the value
        provided in the query string and retrievable via
        `self.value()`.
        """
        if not self.value() or self.value() == 'All':
            return queryset
        return queryset.filter(field__title=self.value())


class DocumentFieldDetectorForm(forms.ModelForm):
    def clean(self):
        detector = DocumentFieldDetector()
        detector.exclude_regexps = self.cleaned_data.get('exclude_regexps') \
            or self.data.get('exclude_regexps')
        detector.include_regexps = self.cleaned_data.get('include_regexps') \
            or self.data.get('include_regexps')
        field = self.cleaned_data.get('field') \
            or self.data.get('field')

        errors = []
        if field:
            detector.field = field
            detector.detected_value = self.cleaned_data.get('detected_value') \
                or self.data.get('detected_value')
            detector.regexps_pre_process_lower = self.cleaned_data.get('regexps_pre_process_lower') \
                or self.data.get('regexps_pre_process_lower')
            errors = detector.check_model()
        for error_msg, exc in errors:
            self.add_error(error_msg, exc)
        return self.cleaned_data


class DocumentFieldDetectorAdmin(admin.ModelAdmin):
    form = DocumentFieldDetectorForm
    list_display = (
        'field', 'detected_value', 'extraction_hint', 'text_part', 'definition_words_',
        'exclude_regexps_', 'include_regexps_', 'regexps_pre_process_lower')
    search_fields = ['field__long_code', 'field__title', 'detected_value', 'extraction_hint', 'include_regexps']
    list_filter = (DocumentTypeListFilter, FieldDetectorListFilter,)

    @staticmethod
    def document_type_code(obj):
        return obj.document_type.code if obj.document_type else None

    document_type_code.admin_order_field = 'document_type'

    @staticmethod
    def field_code(obj):
        return obj.field.code if obj.field else None

    field_code.admin_order_field = 'field'

    @staticmethod
    def include_regexps_(obj):
        return format_html_join('\n', '<pre>{}</pre>',
                                ((r,) for r in
                                 obj.include_regexps.split('\n'))) if obj.field and obj.include_regexps else None

    @staticmethod
    def exclude_regexps_(obj: DocumentFieldDetector):
        return format_html_join('\n', '<pre>{}</pre>',
                                ((r,) for r in
                                 obj.exclude_regexps.split('\n'))) if obj.field and obj.exclude_regexps else None

    @staticmethod
    def definition_words_(obj: DocumentFieldDetector):
        return format_html_join('\n', '<pre>{}</pre>',
                                ((r,) for r in
                                 obj.definition_words.split('\n'))) if obj.field and obj.definition_words else None


class DocumentFieldMultilineRegexDetectorForm(forms.ModelForm):
    csv_content = forms.CharField(widget=EditableTableWidget())

    class Meta:
        model = DocumentFieldMultilineRegexDetector
        fields = ['document_field', 'csv_content', 'extraction_hint', 'text_part', 'regexps_pre_process_lower']
        readonly_fields = ['csv_checksum']


class DocumentFieldMultilineRegexDetectorAdmin(admin.ModelAdmin):
    form = DocumentFieldMultilineRegexDetectorForm
    change_form_template = 'admin/document/documentfieldmultilineregexdetector/change_form.html'
    list_display = ('document_field', 'csv_content_')
    search_fields = ['document_field__long_code', 'document_field__title']
    max_rows = 40

    #class Media:
    #    js = ('js/table_editor.js',)

    @staticmethod
    def document_type_code(obj):
        return obj.document_type.code if obj.document_type else None

    document_type_code.admin_order_field = 'document_type'

    @staticmethod
    def field_code(obj):
        return obj.field.code if obj.field else None

    field_code.admin_order_field = 'field'

    @staticmethod
    def csv_content_(obj):
        if not obj.csv_content:
            return None

        try:
            df = obj.get_as_pandas_df()
        except Exception as e:
            return f'CSV data is corrupted: {e}'

        if df.shape[1] != 2:
            return f'CSV data is corrupted: should contain 2 columns, but has got {df.shape[1]}'

        max_rows = DocumentFieldMultilineRegexDetectorAdmin.max_rows
        row_count = df.shape[0]
        if row_count > max_rows:
            df = df.iloc[:max_rows]
            df = df.append({
                'value': f'... {row_count - max_rows} more items',
                'pattern': ''
            }, ignore_index=True)

        markup = '<table><tr><th>Value</th><th>Pattern</th></tr>\n'
        for i, row in df.iterrows():
            cell_value = html.escape(row[0])
            cell_pattern = html.escape(row[1])
            markup += f'<tr><td>{cell_value}</td><td>{cell_pattern}</td></tr>'
        markup += '</table>'

        return mark_safe(markup)

    def save_model(self, request, obj, form, change):
        obj.update_checksum()
        super().save_model(request, obj, form, change)

    def update_csv_table_row(self, request):
        request_data = json.loads(request.body.decode('utf-8'))
        csv_data = request_data['csvData']
        df = DocumentFieldMultilineRegexDetector.get_csv_as_pandas_df(csv_data)
        new_values = request_data['newValues']
        old_values = request_data['oldValues']

        # delete row
        if request_data['isDelete']:
            for irow, row in df.iterrows():
                if row[0] != old_values[0] or row[1] != old_values[1]:
                    continue
                df.drop([irow], inplace=True)
                df.sort_values('value', inplace=True)
                return JsonResponse({'succeeded': True, 'data': df.to_csv()},
                                    content_type='application/json')
            return JsonResponse({'succeeded': False},
                                content_type='application/json')

        # edit or add or replace a row
        if not new_values or len(new_values) != 2:
            return JsonResponse({'succeeded': False}, content_type='application/json')
        if not all(v for v in new_values):
            return JsonResponse({'succeeded': False}, content_type='application/json')

        # add row
        if request_data['isNewRow']:
            df = df.append({
                'value': new_values[0],
                'pattern': new_values[1]
            }, ignore_index=True)
            df.drop_duplicates(subset='pattern', inplace=True)
        else:
            for __, row in df.iterrows():
                if row[0] != old_values[0] or row[1] != old_values[1]:
                    continue
                row[0] = new_values[0]
                row[1] = new_values[1]
                break
            df.drop_duplicates(subset='pattern', inplace=True)
        df.sort_values('value', inplace=True)

        return JsonResponse({'succeeded': True, 'data': df.to_csv()},
                            content_type='application/json')

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path('update_csv_table_row/',
                 self.update_csv_table_row,
                 name='update_csv_table_row')
        ]
        return my_urls + urls

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        context['update_csv_table_row_url'] = reverse('admin:update_csv_table_row')
        return super().render_change_form(request, context, add, change, form_url, obj)


class FieldValueAdmin(admin.ModelAdmin):
    raw_id_fields = ('document', 'field',)
    list_display = ('document_type', 'document', 'field', 'value', 'user')
    search_fields = ['document__document_type__code', 'document__document_type__title',
                     'document__name', 'field__code', 'field__title', 'value',
                     'modified_by__username', 'modified_by__first_name',
                     'modified_by__last_name']

    @staticmethod
    def document_type(obj):
        return obj.document.document_type if obj.document else None

    @staticmethod
    def field_code(obj):
        return obj.field.code if obj.field else None

    @staticmethod
    def user(obj):
        return obj.modified_by.username if obj.modified_by else None


class FieldAnnotationStatusAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active', 'is_accepted', 'is_rejected')
    search_fields = ['name', 'code']


class FieldAnnotationAdmin(admin.ModelAdmin):
    raw_id_fields = ('document', 'text_unit',)
    list_display = ('document_type', 'document', 'field', 'value', 'location_start',
                    'location_end', 'location_text', 'extraction_hint', 'user', 'status', 'assignee')
    search_fields = ['document__document_type__code', 'document__document_type__title',
                     'document__name', 'field__code', 'field__title', 'value', 'location_text',
                     'extraction_hint', 'modified_by__username', 'modified_by__first_name',
                     'modified_by__last_name', 'status__name',
                     'assignee__username', 'assignee__first_name', 'assignee__last_name']

    @staticmethod
    def document_type(obj):
        return obj.document.document_type if obj.document else None

    @staticmethod
    def field_code(obj):
        return obj.field.code if obj.field else None

    @staticmethod
    def status(obj):
        return obj.status.name

    @staticmethod
    def assignee(obj):
        return obj.assignee.username if obj.assignee else None

    @staticmethod
    def user(obj):
        return obj.modified_by.username if obj.modified_by else None


class FieldAnnotationFalseMatchAdmin(admin.ModelAdmin):
    raw_id_fields = ('document', 'text_unit',)
    list_display = ('document_type', 'document', 'field', 'value', 'location_start',
                    'location_end', 'location_text')
    search_fields = ['document__document_type__code', 'document__document_type__title',
                     'document__name', 'field__code', 'field__title', 'value', 'location_text']

    @staticmethod
    def document_type(obj):
        return obj.document.document_type if obj.document else None

    @staticmethod
    def field_code(obj):
        return obj.field.code if obj.field else None


class FieldAnnotationSavedFilterAdmin(admin.ModelAdmin):
    list_display = ('filter_type', 'project', 'document_type', 'user', 'title', 'display_order')
    search_fields = ['filter_type', 'project__name', 'document_type__code', 'user__username', 'title']


class ExternalFieldValueAdmin(admin.ModelAdmin):
    list_display = ('field_id', 'value', 'extraction_hint')
    search_fields = ('field_id', 'value', 'extraction_hint')


class DocumentFieldInlineFormset(forms.models.BaseInlineFormSet):
    def clean(self):
        field_ids = set()
        dependencies = list()
        order_values = list()
        for form in self.forms:
            document_field = form.cleaned_data.get('document_field')
            if document_field:
                field_ids.add(document_field.pk)
                if document_field.depends_on_fields.count() > 0:
                    dependencies.append(form)
                order = form.cleaned_data.get('order')
                if order in order_values:
                    form.add_error(None, '"Order" value should be unique')
                else:
                    order_values.append(order)
        for form in dependencies:
            document_field = form.cleaned_data['document_field']
            missed_fields = list()
            depends_on_fields = list(document_field.depends_on_fields.all())
            for field in depends_on_fields:
                if field.pk not in field_ids:
                    missed_fields.append(field.code)
            if len(missed_fields) == 1:
                form.add_error(None, 'Field {0} is required for {1} field'.format(missed_fields[0],
                                                                                  document_field.code))
            elif len(missed_fields) > 1:
                form.add_error(None, 'Fields {0} is required for {1} field'.format(', '.join(missed_fields),
                                                                                   document_field.code))


class DocumentFieldFormInline(forms.ModelForm):
    field = forms.ModelChoiceField(
        queryset=DocumentField.objects.all(),
        required=False,
        empty_label=None)

    def __init__(self, *args, **kwargs):
        prefix = kwargs.get('prefix')
        if prefix and kwargs.get('data') and kwargs.get('instance') is None:
            uid = kwargs['data'].get('{0}-uid'.format(prefix))
            if uid:
                kwargs['instance'] = DocumentField.objects.get(pk=uid)
        super().__init__(*args, **kwargs)
        self.base_instance = kwargs.get('instance')
        self._filter_tree = None

        if self.base_instance:
            self.fields['field'].queryset = DocumentField.objects \
                .filter(document_type=self.base_instance.document_type) \
                .order_by('long_code')
            self.fields['field'].initial = self.base_instance
            self.fields['field'].widget.template_name = 'documentfield_admin_select.html'
            self.fields['category'].queryset = self.base_instance.document_type.categories.all()

    def save(self, *args, **kwargs):
        form_instance = self.cleaned_data['field']
        with transaction.atomic():
            if form_instance != self.instance:
                # self.instance.document_type = None
                # self.instance.save()
                self.instance = form_instance
                self.instance.category = self.cleaned_data['category']
                self.instance.order = self.cleaned_data['order']
                # self.instance.document_type = self.cleaned_data['document_type']
            return super().save(*args, **kwargs)


class DocumentFieldInlineAdmin(admin.TabularInline):
    field = forms.ModelChoiceField(
        queryset=DocumentField.objects.all(),
        required=False)

    fields = ['field', 'category', 'order']

    form = DocumentFieldFormInline
    model = DocumentField

    # We were going this inline table to only show the existing fields of this doc type but do not allow
    # adding them or deleting because adding in the current model actually means taking a field from another
    # doc type and putting it into this one. Changing doc type of a field makes all its existing values invalid
    # and they will be deleted.
    #
    # To disable add/delete django has the methods: has_add_permission(), has_delete_permission().
    # But when has_add_permission() returns False the table does not show the last row for some reason.
    #
    # Here is the workaround which makes the table showing only the existing items and no controls for adding:
    # - has_add_permission() returns True;
    # - number of extra rows is set to 0.

    extra = 0
    max_num = 0

    def has_add_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return False


class DocumentTypeForm(ModelFormWithUnchangeableFields):
    UNCHANGEABLE_FIELDS = ('code',)
    CODE_RE = re.compile(r'^[a-z][a-z0-9_]*$')

    search_fields = forms.ModelMultipleChoiceField(
        label='Default Grid Column Headers',
        queryset=DocumentField.objects.all(),
        required=False,
        widget=FilteredSelectMultiple('Default Grid Column Headers', False))

    class Meta:
        model = DocumentType
        fields = '__all__'
        readonly_fields = ('created_by', 'modified_by')

    def clean_code(self):
        field_code = self.cleaned_data['code']

        # validate only new records, skip already existing ones
        if not DocumentType.objects.filter(pk=self.instance.pk).exists() and not self.CODE_RE.match(field_code):
            raise forms.ValidationError('''Field codes must be lowercase, should start with a latin letter and contain 
            only latin letters, digits, and underscores.''')

        return field_code


class DocumentTypeAdmin(ModelAdminWithPrettyJsonField, UsersTasksValidationAdmin):
    list_display = ('code', 'title', 'fields_num', 'user', 'modified_date')
    search_fields = ['code', 'title', 'created_by__username']
    filter_horizontal = ('search_fields',)
    inlines = (DocumentFieldInlineAdmin,)
    form = DocumentTypeForm
    delete_confirmation_template = 'admin/document/documenttype/delete_confirmation.html'
    delete_selected_confirmation_template = 'admin/document/documenttype/delete_selected_confirmation.html'

    fieldsets = [
        ('General', {
            'fields': (
                'created_by', 'modified_by', 'code', 'title', 'editor_type', 'search_fields',),
        }),

        ('Document Import', {
            'fields': (
                'field_code_aliases',),
        }),

        ('Metadata', {
            'fields': (
                'metadata',),
        }),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_clone_view = False

    @staticmethod
    def fields_num(obj):
        return obj.fields.count()

    @staticmethod
    def user(obj):
        return obj.modified_by.username if obj.modified_by else None

    def save_model(self, request, obj, form, change):
        """
        Patched to send a signal to reindex doc type
        """
        super().save_model(request, obj, form, change)
        signals.document_type_changed.send(self.__class__, user=None, document_type=obj)

    @staticmethod
    def _get_deleted_objects(objects, request, admin_site=None):
        """
        Custom method to display related objects
        Otherwise default collector method works too long and throws 504 on FE side
        """
        from apps.project.models import Project
        model_count = dict(
            projects=Project.all_objects.filter(type__in=objects).count(),
            projects_delete_pending=Project.all_objects.filter(type__in=objects, delete_pending=True).count(),
            documents=Document.all_objects.filter(document_type__in=objects).count(),
            documents_delete_pending=Document.all_objects.filter(document_type__in=objects, delete_pending=True).count(),
            document_fields=DocumentField.objects.filter(document_type__in=objects).count(),
            document_field_detectors=DocumentFieldDetector.objects.filter(field__document_type__in=objects).count()
        )
        return [], model_count, [], []

    def get_deleted_objects(self, objs, request):
        return self._get_deleted_objects(objs, request, self.admin_site)

    def warn_related_projects_exist(self, request, related_projects_count):
        msg = f'Selected Document Type(s) has {related_projects_count} related Project(s), ' \
              'please delete related projects first.'
        self.message_user(request, msg, messages.ERROR)
        return HttpResponseRedirect(reverse('admin:document_documenttype_changelist'))

    def _delete_view(self, request, object_id, extra_context):
        """
        Patched to warn a user if related objects exist and deletion is forbidden
        """
        from apps.document.app_vars import ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT
        if not ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT.val:
            from apps.project.models import Project
            related_projects_count = Project.all_objects.filter(type_id=object_id).count()
            if related_projects_count:
                return self.warn_related_projects_exist(request, related_projects_count)
        response = super()._delete_view(request, object_id, extra_context)

        # patch 'model_count' context to get dict in context instead of dict-items
        if response and hasattr(response, 'context_data'):
            context = response.context_data
            context['model_count'] = dict(context['model_count'])
        return response

    def response_delete(self, request, obj_display, obj_id):
        """
        Patch method to force skip messaging a user and substitute with custom message
        see "run_delete_task"
        """
        from apps.document.app_vars import ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT
        orig = self.message_user
        if ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT.val:
            # patch default method to hide default "Success" message and display custom one
            self.message_user = lambda *args, **kwargs: None
        res = super().response_delete(request, obj_display, obj_id)
        self.message_user = orig
        return res

    def delete_selected_action(self, model_admin, request, qs):
        """
        Patched to warn a user if related objects exist and deletion is forbidden
        """
        from apps.document.app_vars import ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT
        if not ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT.val:
            from apps.project.models import Project
            related_projects_count = Project.all_objects.filter(type__in=qs).count()
            if related_projects_count:
                return self.warn_related_projects_exist(request, related_projects_count)

        # patch default method to hide default "Success" message and display custom one
        if ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT.val:
            model_admin.message_user = lambda *args, **kwargs: None

        # patch 'model_count' context to get dict in context instead of dict-items
        response = super().delete_selected_action(model_admin, request, qs)
        if response and hasattr(response, 'context_data'):
            context = response.context_data
            context['model_count'] = dict(context['model_count'])

        storage = messages.get_messages(request)
        storage.used = True

        return response

    def should_run_delete_task(self, objs):
        """
        Separate helper method to detect which method should be done:
        default deletion procedure OR start DeleteDocumentTypes task
        """
        from apps.document.app_vars import ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT
        from apps.project.models import Project

        if isinstance(objs, DocumentType):
            objs = DocumentType.objects.filter(pk=objs.pk)
        elif isinstance(objs, UsersTasksValidationAdmin.QSList):
            objs = objs.qs
        return (ALLOW_REMOVE_DOC_TYPE_WITH_PROJECT.val and Project.all_objects.filter(type__in=objs).exists()) \
               or self.get_user_task_names() is not None

    def run_delete_task(self, request, queryset):
        """
        Separate helper method to run DeleteDocumentTypes task
        """
        if queryset.exists():
            from apps.task.tasks import _call_task
            from apps.document.tasks import DeleteDocumentTypes
            _call_task(DeleteDocumentTypes,
                       document_type_ids=list(queryset.values_list('pk', flat=True)),
                       user_id=request.user.id)
            document_types_str = ', '.join(queryset.values_list('code', flat=True))
            messages.warning(
                request,
                f'Task "Delete Document Types" is started for document types: {document_types_str}.')

    def delete_model(self, request, obj):
        """
        Either delete object OR run task to delete object.
        """
        if self.should_run_delete_task(obj):
            return self.run_delete_task(request, queryset=DocumentType.objects.filter(pk=obj.pk))
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        """
        Either delete objects OR run task to delete objects.
        """
        if self.should_run_delete_task(queryset):
            if isinstance(queryset, UsersTasksValidationAdmin.QSList):
                queryset = queryset.qs
            return self.run_delete_task(request, queryset=queryset)
        return super().delete_queryset(request, queryset)

    def on_object_deleted(self, obj):
        """
        Patched to send a signal to reindex doc type
        """
        # send signal, mostly to remove table in rawdb
        signals.document_type_deleted.send(self.__class__, user=None, document_type=obj)

    def change_view(self, request, object_id, form_url='', extra_context=None, **kwargs):
        extra_context = extra_context or dict()
        extra_context.update(kwargs)
        self.is_clone_view = 'clone' in kwargs
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def response_change(self, request, obj):
        if self.is_clone_view:
            opts = self.model._meta
            msg_dict = {
                'name': opts.verbose_name,
                'obj': format_html('<a href="{}">{}</a>', reverse('admin:document_documenttype_change', args=[obj.pk]), obj),
            }
            msg = format_html(
                _('The {name} "{obj}" was cloned successfully.'),
                **msg_dict
            )
            self.message_user(request, msg, messages.SUCCESS)
            return self.response_post_save_change(request, obj)
        return super().response_change(request, obj)

    def save_form(self, request, form, change):
        if self.is_clone_view:
            from apps.document.api.v1 import DocumentTypeViewSet
            new_type = DocumentTypeViewSet.clone_type(
                source_document_type=form.instance,
                code=form.cleaned_data['code'],
                title=form.cleaned_data['title'])
            return new_type
        return super().save_form(request, form, change)

    def get_form(self, request, obj=None, **kwargs):
        if self.is_clone_view:
            from apps.document.forms import CloneDocumentTypeForm
            return CloneDocumentTypeForm
        return super().get_form(request, obj=None, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if self.is_clone_view:
            fieldsets = [
                (f'Clone Document Type: {obj}', {'fields': ('code', 'title')})
            ]
            return fieldsets
        return self.fieldsets

    def get_inline_instances(self, request, obj=None):
        if self.is_clone_view:
            return []
        return super().get_inline_instances(request, obj=obj)

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path('<str:object_id>/clone/',
                 self.change_view,
                 kwargs={'clone': True},
                 name='clone_document_type')]
        return my_urls + urls


class DocumentPropertyAdmin(admin.ModelAdmin):
    list_display = ('document', 'key', 'value')
    search_fields = ['document__name', 'key', 'value']


class DocumentRelationAdmin(admin.ModelAdmin):
    list_display = ('document_a', 'document_b', 'relation_type')
    search_fields = ['document_a__name', 'document_a__name', 'relation_type']


class TextUnitTextInline(admin.TabularInline):
    model = TextUnitText


class TextUnitAdmin(DjangoQLSearchMixin, admin.ModelAdmin):
    list_display = ('document', 'unit_ref', 'unit_type', 'language')
    search_fields = ('document__name', 'unit_type', 'language')
    inlines = [TextUnitTextInline]
    last_request = None

    change_list_template = 'admin/document/text_unit/change_list.html'

    def get_search_results(self, request, queryset, search_term):
        return get_unique_search_results(
            self, request, queryset, search_term)

    def get_queryset(self, request):
        self.last_request = request
        req_path = request.path.strip('/')
        if req_path.endswith('change'):
            self.djangoql_completion = False
        else:
            self.djangoql_completion = True
        return TextUnit.objects

    def unit_ref(self, obj):
        url = self.full_reverse('document:text-unit-detail', args=[obj.pk])
        return mark_safe(f'<a href="{url}"><i class="fa fa-external-link"></i>{obj.pk}</a>')

    unit_ref.short_description = 'ID'

    def full_reverse(self, *args, **kwargs):
        return full_reverse(*args, **kwargs, request=self.last_request)


class TextUnitRelatedAdmin(admin.ModelAdmin):
    raw_id_fields = ('text_unit',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related('text_unit', 'text_unit__document', 'text_unit__textunittext')
        return qs


class TextUnitTextAdmin(TextUnitRelatedAdmin):
    search_fields = ('text_unit__id', 'text_unit__unit_type')


class TextUnitPropertyAdmin(TextUnitRelatedAdmin):
    list_display = ('pk', 'text_unit', 'key', 'value')
    search_fields = ['key', 'value']


class TextUnitTagAdmin(TextUnitRelatedAdmin):
    list_display = ('text_unit', 'tag')
    search_fields = ('text_unit__unit_type', 'tag')


class TextUnitNoteAdmin(TextUnitRelatedAdmin, SimpleHistoryAdmin):
    list_display = ('text_unit', 'timestamp')
    search_fields = ('text_unit__unit_type', 'timestamp', 'note')


class DocumentNoteAdmin(SimpleHistoryAdmin):
    list_display = ('document', 'timestamp')
    search_fields = ('document__name', 'timestamp', 'note')


class ClassifierModelAdmin(SimpleHistoryAdmin):
    list_display = ('document_field',)
    search_fields = ('document_field__long_code', 'document_field__title')


class DocumentFieldCategoryForm(forms.ModelForm):
    class Meta:
        model = DocumentFieldCategory
        fields = ['name', 'document_type', 'order', 'export_key']

    fields = forms.ModelMultipleChoiceField(
        queryset=DocumentField.objects.all(),
        label='Select Type Fields',
        required=False,
        widget=FilteredSelectMultiple('fields', False))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['document_type'].required = True
        self.fields['export_key'].required = False
        self.fields['export_key'].disabled = True
        if self.instance.pk:
            self.fields['fields'].initial = self.instance.documentfield_set.all()
        else:
            self.fields['fields'].initial = DocumentField.objects.none()

    def clean(self):
        fields = self.cleaned_data.get('fields')
        document_type = self.cleaned_data.get('document_type')
        if document_type and fields and set(fields.values_list('document_type', flat=True)) != {document_type.pk}:
            self.add_error('fields', 'Document Field must be from the same Document Type selected above.')

    def save(self, *args, **kwargs):
        # TODO: Wrap reassignments into transaction
        # NOTE: Previously assigned DocumentFieldCategory is silently reset
        instance = super().save(commit=True)
        self.fields['fields'].initial.update(category=None)
        self.cleaned_data['fields'].update(category=instance)
        return instance

    def save_m2m(self, *args, **kwargs):
        pass


class DocumentFieldCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'document_type', 'order')
    search_fields = ('name', 'document_type__title')
    form = DocumentFieldCategoryForm


class DocumentFieldFamilyInline(admin.TabularInline):
    model = DocumentField


class DocumentFieldFamilyForm(ModelFormWithUnchangeableFields):
    UNCHANGEABLE_FIELDS = ('code',)
    R_FIELD_CODE = re.compile(r'^[a-z][a-z0-9_]*$')
    inlines = [DocumentFieldFamilyInline]

    fields = forms.ModelMultipleChoiceField(
        queryset=DocumentField.objects.all(),
        label='Select Type Fields',
        required=False,
        widget=FilteredSelectMultiple('fields', False))

    class Meta:
        model = DocumentFieldFamily
        fields = ['title', 'code']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['fields'].initial = self.instance.documentfield_set.all()
            self.fields['code'].help_text = 'This field is unchangable.'
        else:
            self.fields['fields'].initial = DocumentField.objects.none()
            self.fields['code'].help_text = 'Leave it blank to auto-create from title.'

    def save(self, *args, **kwargs):
        # TODO: Wrap reassignments into transaction
        # NOTE: Previously assigned DocumentFieldCategory are silently reset
        instance = super().save(commit=True)
        self.fields['fields'].initial.update(family=None)
        self.cleaned_data['fields'].update(family=instance)
        return instance

    def save_m2m(self, *args, **kwargs):
        pass

    def clean_title(self):
        title = self.cleaned_data['title']
        if DocumentFieldFamily.objects.exclude(pk=self.instance.pk).filter(title__iexact=title):
            self.add_error('title', 'DocumentFieldFamily with similar title already exists.')
        return title

    def clean_code(self):
        code = self.cleaned_data['code']
        if code:
            if not self.R_FIELD_CODE.match(code):
                self.add_error('code', '''Document Field Family codes must be lowercase, 
                should start with a latin letter and contain 
                only latin letters, digits, and underscores.''')
            if DocumentFieldFamily.objects.exclude(pk=self.instance.pk).filter(code__iexact=code):
                self.add_error('code', 'Document Field Family with the same code already exists.')
        return code


class DocumentFieldFamilyAdmin(admin.ModelAdmin):
    list_display = ('title', 'code')
    search_fields = ('title', 'code')
    form = DocumentFieldFamilyForm


admin.site.register(Document, DocumentAdmin)
admin.site.register(DocumentMetadata, DocumentMetadataAdmin)
admin.site.register(DocumentText, DocumentTextAdmin)
admin.site.register(SoftDeleteDocument, SoftDeleteDocumentAdmin)
admin.site.register(DocumentField, DocumentFieldAdmin)
admin.site.register(DocumentFieldDetector, DocumentFieldDetectorAdmin)
admin.site.register(DocumentFieldMultilineRegexDetector, DocumentFieldMultilineRegexDetectorAdmin)
admin.site.register(FieldValue, FieldValueAdmin)
admin.site.register(FieldAnnotationStatus, FieldAnnotationStatusAdmin)
admin.site.register(FieldAnnotation, FieldAnnotationAdmin)
admin.site.register(FieldAnnotationFalseMatch, FieldAnnotationFalseMatchAdmin)
admin.site.register(FieldAnnotationSavedFilter, FieldAnnotationSavedFilterAdmin)
admin.site.register(ExternalFieldValue, ExternalFieldValueAdmin)
admin.site.register(ClassifierModel, ClassifierModelAdmin)
admin.site.register(DocumentType, DocumentTypeAdmin)
admin.site.register(DocumentRelation, DocumentRelationAdmin)
admin.site.register(DocumentProperty, DocumentPropertyAdmin)
admin.site.register(TextUnitProperty, TextUnitPropertyAdmin)
# admin.site.register(TextUnit, TextUnitAdmin)
admin.site.register(TextUnitText, TextUnitTextAdmin)
admin.site.register(TextUnitTag, TextUnitTagAdmin)
admin.site.register(TextUnitNote, TextUnitNoteAdmin)
admin.site.register(DocumentNote, DocumentNoteAdmin)
admin.site.register(DocumentFieldCategory, DocumentFieldCategoryAdmin)
admin.site.register(DocumentFieldFamily, DocumentFieldFamilyAdmin)
