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

import time
from urllib.parse import quote
from collections import defaultdict
from typing import Dict, List, Set, Generator, Any

import rest_framework.views
import pandas as pd
from allauth.socialaccount import providers
from allauth.socialaccount.models import SocialApp

from django.conf.urls import url
from django.db import transaction
from django.db.models import Q, F
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

import apps.common.mixins
from apps.common.errors import APIRequestError
from apps.common.url_utils import as_bool, as_int, as_int_list, as_str_list
from apps.document.models import Document, DocumentType
from apps.document.scheme_migrations.scheme_migration import MIGRATION_TAGS, CURRENT_VERSION
from apps.project.models import Project
from apps.rawdb.constants import FT_COMMON_FILTER, FT_USER_DOC_GRID_CONFIG, \
    FIELD_CODES_SHOW_BY_DEFAULT_NON_GENERIC, FIELD_CODES_SHOW_BY_DEFAULT_GENERIC, \
    FIELD_CODES_HIDE_FROM_CONFIG_API, FIELD_CODE_ANNOTATION_SUFFIX
from apps.rawdb.field_value_tables import get_columns, get_annotation_columns, \
    query_documents, DocumentQueryResults
from apps.rawdb.models import SavedFilter
from apps.rawdb.rawdb.query_parsing import parse_order_by
from apps.rawdb.rawdb.rawdb_field_handlers import ColumnDesc

__author__ = "ContraxSuite, LLC; LexPredict, LLC"
__copyright__ = "Copyright 2015-2020, ContraxSuite, LLC"
__license__ = "https://github.com/LexPredict/lexpredict-contraxsuite/blob/1.7.0/LICENSE"
__version__ = "1.7.0"
__maintainer__ = "LexPredict, LLC"
__email__ = "support@contraxsuite.com"


def _column_to_dto(column: ColumnDesc, add_query_syntax: bool = False) -> Dict:
    res = {
        'column': column.name,
        'title': column.title,
        'type': column.value_type.value,
    }
    if add_query_syntax:
        res['query_syntax'] = column.get_field_filter_syntax_hint()
    return res


def _document_type_schema_to_dto(document_type: DocumentType, columns: List[ColumnDesc], default_columns: Set[str],
                                 add_query_syntax: bool = False) -> Dict:
    document_type_data = {
        'code': document_type.code,
        'title': document_type.title,
        'columns': [_column_to_dto(column, add_query_syntax) for column in columns],
        'default_columns': default_columns
    }

    return document_type_data


class RawDBConfigAPIView(apps.common.mixins.APILoggingMixin, rest_framework.views.APIView):
    def get(self, request, *args, **kwargs):
        start = time.time()
        add_query_syntax = as_bool(request.GET, 'add_query_syntax', False)

        document_type_schema = dict()
        for document_type in DocumentType.objects.all():
            columns = get_columns(document_type,
                                  include_generic=document_type.is_generic())  # type: List[ColumnDesc]
            columns = [c for c in columns if c.field_code not in FIELD_CODES_HIDE_FROM_CONFIG_API]

            system_fields = FIELD_CODES_SHOW_BY_DEFAULT_GENERIC \
                if document_type.is_generic() else FIELD_CODES_SHOW_BY_DEFAULT_NON_GENERIC
            search_fields = set(document_type.search_fields.all().values_list('code', flat=True))

            default_columns = {c.name for c in columns
                               if c.field_code not in FIELD_CODES_HIDE_FROM_CONFIG_API and
                               (c.field_code in system_fields or c.field_code in search_fields)}

            document_type_schema[document_type.code] = _document_type_schema_to_dto(document_type,
                                                                                    columns,
                                                                                    default_columns,
                                                                                    add_query_syntax)

        common_filters_by_document_type = defaultdict(list)  # type: Dict[List]

        for document_type_code, filter_id, title, display_order in SavedFilter.objects \
                .filter(project_id__isnull=True, filter_type=FT_COMMON_FILTER) \
                .filter(Q(user__isnull=True) | Q(user=request.user)) \
                .values_list('document_type__code', 'id', 'title', 'display_order'):
            common_filters_by_document_type[document_type_code].append({
                'id': filter_id,
                'title': title,
                'display_order': display_order
            })

        common_filters_by_project = defaultdict(list)  # type: Dict[List]

        for project_id, filter_id, title, display_order in SavedFilter.objects \
                .filter(project_id__isnull=False, filter_type=FT_COMMON_FILTER) \
                .filter(Q(user__isnull=True) | Q(user=request.user)) \
                .values_list('project_id', 'id', 'title', 'display_order'):
            common_filters_by_project[project_id].append({
                'id': filter_id,
                'title': title,
                'display_order': display_order
            })

        user_doc_grid_configs_by_project = defaultdict(list)  # type: Dict[List]

        for project_id, columns, column_filters, order_by in SavedFilter.objects \
                .filter(user=request.user, project_id__isnull=False, filter_type=FT_USER_DOC_GRID_CONFIG) \
                .filter(document_type_id=F('project__type_id')) \
                .order_by('pk') \
                .values_list('project_id', 'columns', 'column_filters', 'order_by'):
            user_doc_grid_configs_by_project[project_id] = {
                'columns': columns,
                'column_filters': column_filters,
                'order_by': order_by
            }

        return Response({
            'document_type_schema': document_type_schema,
            'common_filters_by_document_type': common_filters_by_document_type,
            'common_filters_by_project': common_filters_by_project,
            'user_doc_grid_configs_by_project': user_doc_grid_configs_by_project,
            'time': time.time() - start,
            'scheme_migrations': {
                'migrations': MIGRATION_TAGS,
                'current_version': CURRENT_VERSION
            }
        })


class SocialAccountsAPIView(apps.common.mixins.APILoggingMixin, rest_framework.views.APIView):
    permission_classes = (AllowAny,)
    authentication_classes = []

    def get(self, request, *_args, **_kwargs):
        root_url = request.build_absolute_uri('/')
        next_val = quote(root_url.rstrip('/') + '/#/?redirect_oauth=true')

        social_apps = SocialApp.objects.all()
        social_app_data = []
        for app in social_apps:  # type: SocialApp
            provider = providers.registry.by_id(app.provider)
            provider_url = provider.get_login_url(None)
            provider_url = f'{provider_url.rstrip()}?next={next_val}'
            social_app_data.append({
                'name': app.name,
                'provider': app.provider,
                'login_url': provider_url
            })

        return Response({
            'social_accounts': social_app_data
        })


class DocumentsAPIView(rest_framework.views.APIView):
    URL_PARAM_PREFIX_FILTER = 'where_'

    MAX_RETURNED_DOCUMENTS_JSON = 200

    FMT_JSON = 'json'
    FMT_CSV = 'csv'
    FMT_XLSX = 'xlsx'

    def get(self, request, document_type_code: str, *_args, **_kwargs):
        start = time.time()
        try:
            document_type = DocumentType.objects.get(code=document_type_code)

            project_ids = as_int_list(request.GET, 'project_ids')  # type: List[int]

            columns = as_str_list(request.GET, 'columns')

            include_annotations = as_bool(request.GET, 'associated_text')
            if include_annotations:
                all_annotation_columns = get_annotation_columns(document_type)
                columns += [i.field_code for i in all_annotation_columns
                            if i.field_code.rstrip(FIELD_CODE_ANNOTATION_SUFFIX) in columns]

            fmt = request.GET.get('fmt') or self.FMT_JSON
            as_zip = request.GET.get('as_zip') == 'true'

            offset = as_int(request.GET, 'offset', None)
            if offset is not None and offset < 0:
                offset = None

            limit = as_int(request.GET, 'limit', None)
            if limit is not None and limit <= 0:
                limit = None

            # For json output we limit number of returned documents because we dont use streaming response for JSON
            # and want to keep it fast.
            if fmt == self.FMT_JSON and self.MAX_RETURNED_DOCUMENTS_JSON is not None \
                    and (limit is None or limit > self.MAX_RETURNED_DOCUMENTS_JSON):
                limit = self.MAX_RETURNED_DOCUMENTS_JSON

            saved_filters = as_int_list(request.GET, 'saved_filters')  # type: List[int]

            column_filters = list()
            for param, value in request.GET.items():  # type: str, str
                if param.startswith(self.URL_PARAM_PREFIX_FILTER):
                    column_filters.append((param[len(self.URL_PARAM_PREFIX_FILTER):], value))

            order_by = request.GET.get('order_by') or None  # type: str
            order_by = parse_order_by(order_by) if order_by else None

            save_filter = as_bool(request.GET, 'save_filter', False)  # type: bool

            return_reviewed = as_bool(request.GET, 'return_reviewed', False)
            return_total = as_bool(request.GET, 'return_total', True)
            return_data = as_bool(request.GET, 'return_data', True)
            ignore_errors = as_bool(request.GET, 'ignore_errors', True)

            if project_ids and save_filter:
                column_filters_dict = {c: f for c, f in column_filters}
                for project_id in project_ids:
                    with transaction.atomic():
                        obj = SavedFilter.objects.create(user=request.user,
                                                         document_type=document_type,
                                                         filter_type=FT_USER_DOC_GRID_CONFIG,
                                                         project_id=project_id,
                                                         columns=columns,
                                                         column_filters=column_filters_dict,
                                                         title=None,
                                                         order_by=[(column, direction.value)
                                                                   for
                                                                   column, direction in
                                                                   order_by] if order_by
                                                         else None
                                                         )
                        SavedFilter.objects.filter(user=request.user,
                                                   filter_type=FT_USER_DOC_GRID_CONFIG,
                                                   project_id=project_id) \
                            .exclude(pk=obj.pk) \
                            .delete()

            # show_unprocessed = as_bool(request.GET, 'show_unprocessed', False)
            # if show_unprocessed is False:
            #     column_filters.append((FIELD_CODE_DOC_PROCESSED, 'true'))
            total_documents_query = Document.objects.filter(
                document_type=document_type)
            if project_ids:
                total_documents_query = total_documents_query.filter(project_id__in=project_ids)
            total_documents_of_type = total_documents_query.count()

            columns_to_query = columns
            if columns_to_query:
                columns_to_query = self.leave_unique_values(['document_id', 'document_name'] + columns)

            query_results = query_documents(
                requester=request.user,
                document_type=document_type,
                project_ids=project_ids,
                column_names=columns_to_query,  # columns,
                saved_filter_ids=saved_filters,
                column_filters=column_filters,
                order_by=order_by,
                offset=offset,
                limit=limit,
                return_documents=return_data,
                return_reviewed_count=return_reviewed,
                return_total_count=return_total,
                ignore_errors=ignore_errors,
                include_annotation_fields=True)  # type: DocumentQueryResults

            # get assignees stats
            assignees_query_results = query_documents(
                requester=request.user,
                document_type=document_type,
                project_ids=project_ids,
                column_names=['document_id', 'assignee_name', 'assignee_id'],
                saved_filter_ids=saved_filters,
                column_filters=column_filters,
                return_documents=True,
                return_reviewed_count=False)  # type: DocumentQueryResults

            df = pd.DataFrame(assignees_query_results.fetch_dicts())
            if df.empty:
                query_results.assignees = []
            else:
                df = df.groupby(['assignee_id', 'assignee_name']).agg({
                    'document_id': {'document_ids': lambda x: list(x), 'documents_count': 'count'}})
                if df.empty:
                    query_results.assignees = []
                else:
                    df.columns = df.columns.droplevel()
                    df = df.reset_index()
                    df['assignee_id'] = df['assignee_id'].astype(int)
                    query_results.assignees = df.to_dict('records')

            query_results.unfiltered_count = total_documents_of_type

            if fmt in {self.FMT_XLSX, self.FMT_CSV} and not return_data:
                raise APIRequestError('Export to csv/xlsx requested with return_data=false')

            if fmt == self.FMT_CSV:
                return query_results.to_csv(as_zip=as_zip)
            elif fmt == self.FMT_XLSX:
                return query_results.to_xlsx(as_zip=as_zip)
            else:
                if query_results is None:
                    return Response({'time': time.time() - start})
                query_dict = query_results.to_json(time_start=start)
                if columns and 'items' in query_dict:
                    columns_to_remove = []
                    if 'document_id' not in columns:
                        columns_to_remove.append('document_id')
                    query_dict['items'] = self.expand_items(
                        query_dict['items'], columns_to_remove)
                return Response(query_dict)
        except APIRequestError as e:
            return e.to_response()
        except Exception as e:
            return APIRequestError(message='Unable to process request', caused_by=e, http_status_code=500).to_response()

    @staticmethod
    def expand_items(items: Generator[Dict[str, Any], None, None],
                     columns_to_remove: List[str]) -> Generator[Dict[str, Any], None, None]:
        for item in items:
            if 'document_id' in item:
                item['doc_identifier'] = item['document_id']
            for cl in columns_to_remove:
                if cl in item:
                    del item[cl]
            yield item

    @staticmethod
    def leave_unique_values(lst: List[str]) -> List[str]:
        seen = set()
        seen_add = seen.add
        return [x for x in lst if not (x in seen or seen_add(x))]

    def post(self, request, document_type_code: str, *args, **kwargs):
        request.GET = request.GET.copy()
        request.GET.update(request.data)
        request.GET.update(request.POST)
        return self.get(request, document_type_code, *args, **kwargs)

    @classmethod
    def simulate_get(cls, user, project, return_ids=True, use_saved_filter=True):
        """
        Re-use DocumentsAPIView and SavedFilter to fetch all filtered project documents
        """
        # TODO: Do not simulate http requests, use query_documents()
        filters = order_by = None

        if isinstance(project, (int, str)):
            project = Project.objects.get(pk=project)

        if use_saved_filter:
            try:
                saved_filter = SavedFilter.objects.get(project_id=project.id, user=user)
                filters = saved_filter.column_filters
                order_by = saved_filter.order_by
            except:
                pass

        if order_by is None:
            order_by = [['document_name', 'asc']]

        class FakeRequest:
            def __init__(the_self):
                the_self.user = user
                the_self.GET = {'columns': 'document_id',
                                'project_ids': str(project.id),
                                'order_by': ','.join('{}:{}'.format(field, order) for field, order in order_by),
                                'save_filter': 'false'}
                if filters:
                    for field_name, condition in filters.items():
                        the_self.GET['where_{}'.format(field_name)] = condition

        request = FakeRequest()
        doc_type_code = project.type.code
        view = cls(request=request, format_kwarg=doc_type_code)
        view.MAX_RETURNED_DOCUMENTS_JSON = None
        resp = view.get(request=request, document_type_code=doc_type_code).data['items']
        ids = [i['document_id'] for i in resp]

        if return_ids:
            return ids
        return Document.all_objects.filter(id__in=ids)


class ProjectStatsAPIView(rest_framework.views.APIView):
    def get(self, request, project_id: int, *_args, **_kwargs):
        try:
            start = time.time()
            project = Project.objects.filter(pk=project_id).select_related('type').first()
            if not project:
                return Response({'error': 'Project not found'}, status=404)

            saved_filters = as_int_list(request.GET, 'saved_filters')  # type: List[int]

            query_results = query_documents(requester=request.user,
                                            document_type=project.type,
                                            project_ids=[project.pk],
                                            saved_filter_ids=saved_filters,
                                            return_reviewed_count=True,
                                            return_documents=False,
                                            return_total_count=True,
                                            include_annotation_fields=True)  # type: DocumentQueryResults
            if not query_results:
                return Response({'time': time.time() - start})

            return Response(query_results.to_json(time_start=start))
        except APIRequestError as e:
            return e.to_response()
        except Exception as e:
            return APIRequestError(message='Unable to process request', caused_by=e, http_status_code=500).to_response()


urlpatterns = [
    url(r'documents/(?P<document_type_code>[^/]+)/$', DocumentsAPIView.as_view(), name='documents'),
    url(r'project_stats/(?P<project_id>\d+)/$', ProjectStatsAPIView.as_view(), name='project_stats'),
    url(r'config/$', RawDBConfigAPIView.as_view(), name='config'),
    url(r'social_accounts/$', SocialAccountsAPIView.as_view(), name='social_accounts'),
]
