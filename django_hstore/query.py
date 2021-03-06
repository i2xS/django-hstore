from __future__ import unicode_literals, absolute_import

from decimal import Decimal
from datetime import date, time, datetime

from django import VERSION
from django.db import transaction
from django.utils import six
from django.db.models.query import QuerySet
from django.db.models.sql.constants import SINGLE
from django.db.models.sql.datastructures import EmptyResultSet
from django.db.models.sql.query import Query
from django.db.models.sql.subqueries import UpdateQuery
from django.db.models.sql.where import EmptyShortCircuit, WhereNode

try:
    from django.contrib.gis.db.models.query import GeoQuerySet
    from django.contrib.gis.db.models.sql.query import GeoQuery
    from django.contrib.gis.db.models.sql.where import \
        GeoWhereNode, GeoConstraint
    GEODJANGO_INSTALLED = True
except:
    GEODJANGO_INSTALLED = False


class literal_clause(object):
    def __init__(self, sql, params):
        self.clause = (sql, params)

    def as_sql(self, qn, connection):
        return self.clause


try:
    from django.db.models.query_utils import QueryWrapper  # django >= 1.4
except ImportError:
    from django.db.models.sql.where import QueryWrapper  # django <= 1.3


def select_query(method):

    def selector(self, *args, **params):
        query = self.query.clone()
        query.default_cols = False
        query.clear_select_fields()
        return method(self, query, *args, **params)

    return selector


def update_query(method):

    def updater(self, *args, **params):
        self._for_write = True
        query = method(self, self.query.clone(UpdateQuery), *args, **params)
        forced_managed = False
        if not transaction.is_managed(using=self.db):
            transaction.enter_transaction_management(using=self.db)
            forced_managed = True
        try:
            rows = query.get_compiler(self.db).execute_sql(None)
            if forced_managed:
                transaction.commit(using=self.db)
            else:
                transaction.commit_unless_managed(using=self.db)
        finally:
            if forced_managed:
                transaction.leave_transaction_management(using=self.db)
        self._result_cache = None
        return rows
    updater.alters_data = True
    return updater


def get_cast_for_param(value_annot, key):
    if not isinstance(value_annot, dict):
        return ''

    if value_annot[key] in (True, False):
        return '::boolean'
    elif issubclass(value_annot[key], datetime):
        return '::timestamp'
    elif issubclass(value_annot[key], date):
        return '::date'
    elif issubclass(value_annot[key], time):
        return '::time'
    elif issubclass(value_annot[key], six.integer_types):
        return '::bigint'
    elif issubclass(value_annot[key], float):
        return '::float8'
    elif issubclass(value_annot[key], Decimal):
        return '::numeric'
    else:
        return ''


def get_value_annotations(param):
    # We need to store the actual value for booleans, not just the type, for isnull
    get_type = lambda v: v if isinstance(v, bool) else type(v)
    return dict((key, get_type(subvalue)) for key, subvalue in six.iteritems(param))


class HStoreWhereNode(WhereNode):

    def add(self, data, *args, **kwargs):
        # WhereNode will convert params into strings, so we need to record
        # the type of the params as part of the value_annotation before calling
        # the super class
        if not isinstance(data, (list, tuple)):
            return super(HStoreWhereNode, self).add(data, *args, **kwargs)

        original_value = data[2]

        if isinstance(original_value, dict):
            len_children = len(self.children) if self.children else 0

            value_annot = get_value_annotations(original_value)

            # We should be able to get the normal child node here, but it is not returned in Django 1.5
            super(HStoreWhereNode, self).add(data, *args, **kwargs)

            # We also need to place the type annotation into self.children
            # It will either be the last item in the last child, or be the last child
            # We can tell which by comparing the lengths before and after calling the super method
            if len_children < len(self.children):
                child = self.children[-1]

                obj, lookup_type, _, value = child
                annotated_child = (obj, lookup_type, value_annot, value)

                self.children[-1] = annotated_child
            else:
                child = self.children[-1][-1]

                obj, lookup_type, _, value = child
                annotated_child = (obj, lookup_type, value_annot, value)

                self.children[-1][-1] = annotated_child
        else:
            return super(HStoreWhereNode, self).add(data, *args, **kwargs)

    # FIXME: this method shuld be more clear.
    def make_atom(self, child, qn, connection):
        lvalue, lookup_type, value_annot, param = child
        kwargs = {'connection': connection} if VERSION[:2] >= (1, 3) else {}

        if lvalue and lvalue.field and hasattr(lvalue.field, 'db_type') and lvalue.field.db_type(**kwargs) == 'hstore':
            try:
                lvalue, params = lvalue.process(lookup_type, param, connection)
            except EmptyShortCircuit:
                raise EmptyResultSet()

            field = self.sql_for_columns(lvalue, qn, connection)

            if lookup_type == 'exact':
                if isinstance(param, dict):
                    return ('{0} = %s'.format(field), [param])

                raise ValueError('invalid value')

            elif lookup_type in ('gt', 'gte', 'lt', 'lte'):
                if isinstance(param, dict):
                    sign = (lookup_type[0] == 'g' and '>%s' or '<%s') % (lookup_type[-1] == 'e' and '=' or '')
                    param_keys = list(param.keys())
                    conditions = []

                    for key in param_keys:
                        cast = get_cast_for_param(value_annot, key)
                        conditions.append('(%s->\'%s\')%s %s %%s' % (field, key, cast, sign))

                    return (" AND ".join(conditions), param.values())

                raise ValueError('invalid value')

            elif lookup_type in ['contains', 'icontains']:
                if isinstance(param, dict):
                    values = list(param.values())
                    keys = list(param.keys())

                    if len(values) == 1 and isinstance(values[0], (list, tuple)):
                        # Can't cast here because the list could contain multiple types
                        return ('%s->\'%s\' = ANY(%%s)' % (field, keys[0]), [[str(x) for x in values[0]]])
                    elif len(keys) == 1 and len(values) == 1:
                        # Retrieve key and compare to param instead of using '@>' in order to cast hstore value
                        cast = get_cast_for_param(value_annot, keys[0])
                        return ('(%s->\'%s\')%s = %%s' % (field, keys[0], cast), [values[0]])

                    return ('%s @> %%s' % field, [param])

                elif isinstance(param, (list, tuple)):
                    if len(param) == 0:
                        raise ValueError('invalid value')

                    if len(param) < 2:
                        return ('%s ? %%s' % field, [param[0]])

                    if param:
                        return ('%s ?& %%s' % field, [param])

                    raise ValueError('invalid value')

                elif isinstance(param, six.string_types):
                    # if looking for a string perform the normal text lookup
                    # that is: look for occurence of string in all the keys
                    pass

                else:
                    raise ValueError('invalid value')

            elif lookup_type == 'isnull':
                if isinstance(param, dict):
                    param_keys = list(param.keys())
                    conditions = []

                    for key in param_keys:
                        op = 'IS NULL' if value_annot[key] else 'IS NOT NULL'
                        conditions.append('(%s->\'%s\') %s' % (field, key, op))

                    return (" AND ".join(conditions), [])
                # do not perform any special format
                return super(HStoreWhereNode, self).make_atom(child, qn, connection)

            else:
                raise TypeError('invalid lookup type')

        return super(HStoreWhereNode, self).make_atom(child, qn, connection)

    make_hstore_atom = make_atom


if GEODJANGO_INSTALLED:
    class HStoreGeoWhereNode(HStoreWhereNode, GeoWhereNode):

        def make_atom(self, child, qn, connection):
            lvalue, lookup_type, value_annot, params_or_value = child

            # if spatial query
            if isinstance(lvalue, GeoConstraint):
                return GeoWhereNode.make_atom(self, child, qn, connection)

            # else might be an HSTORE query
            return HStoreWhereNode.make_atom(self, child, qn, connection)


class HStoreQuery(Query):

    def __init__(self, model):
        super(HStoreQuery, self).__init__(model, HStoreWhereNode)


if GEODJANGO_INSTALLED:
    class HStoreGeoQuery(GeoQuery, Query):

        def __init__(self, *args, **kwargs):
            model = kwargs.pop('model', None) or args[0]
            super(HStoreGeoQuery, self).__init__(model, HStoreGeoWhereNode)


class HStoreQuerySet(QuerySet):

    def __init__(self, model=None, query=None, using=None, *args, **kwargs):
        query = query or HStoreQuery(model)
        super(HStoreQuerySet, self).__init__(model=model, query=query, using=using, *args, **kwargs)

    @select_query
    def hkeys(self, query, attr):
        """
        Enumerates the keys in the specified hstore.
        """
        query.add_extra({'_': 'akeys("%s")' % attr}, None, None, None, None, None)
        result = query.get_compiler(self.db).execute_sql(SINGLE)
        return (result[0] if result else [])

    @select_query
    def hpeek(self, query, attr, key):
        """
        Peeks at a value of the specified key.
        """
        query.add_extra({'_': '%s -> %%s' % attr}, [key], None, None, None, None)
        result = query.get_compiler(self.db).execute_sql(SINGLE)
        if result and result[0]:
            field = self.model._meta.get_field_by_name(attr)[0]
            return field._value_to_python(result[0])

    @select_query
    def hslice(self, query, attr, keys):
        """
        Slices the specified key/value pairs.
        """
        query.add_extra({'_': 'slice("%s", %%s)' % attr}, [keys], None, None, None, None)
        result = query.get_compiler(self.db).execute_sql(SINGLE)
        if result and result[0]:
            field = self.model._meta.get_field_by_name(attr)[0]
            return dict((key, field._value_to_python(value)) for key, value in result[0].items())
        return {}

    @update_query
    def hremove(self, query, attr, keys):
        """
        Removes the specified keys in the specified hstore.
        """
        value = QueryWrapper('delete("%s", %%s)' % attr, [keys])
        field, model, direct, m2m = self.model._meta.get_field_by_name(attr)
        query.add_update_fields([(field, None, value)])
        return query

    @update_query
    def hupdate(self, query, attr, updates):
        """
        Updates the specified hstore.
        """
        value = QueryWrapper('"%s" || %%s' % attr, [updates])
        field, model, direct, m2m = self.model._meta.get_field_by_name(attr)
        query.add_update_fields([(field, None, value)])
        return query


if GEODJANGO_INSTALLED:
    class HStoreGeoQuerySet(HStoreQuerySet, GeoQuerySet):

        def __init__(self, model=None, query=None, using=None, **kwargs):
            query = query or HStoreGeoQuery(model)
            super(HStoreGeoQuerySet, self).__init__(model=model, query=query, using=using, **kwargs)
