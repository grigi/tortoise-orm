import datetime
from pypika import Table

from tortoise import fields


class BaseExecutor:
    def __init__(self, model, db=None, prefetch_map=None):
        self.model = model
        self.db = db
        self.connection = None
        self.prefetch_map = prefetch_map
        self._prefetch_queries = {}

    async def execute_select(self, query):
        self.connection = await self.db.get_single_connection()
        raw_results = await self.connection.execute_query(str(query))
        instance_list = [self.model(**row) for row in raw_results]
        await self._execute_prefetch_queries(instance_list)
        await self.db.release_single_connection(self.connection)
        self.connection = None
        return instance_list

    async def execute_insert(self, instance):
        # Insert should implement returning new id to saved object
        # Each db has it's own methods for it, so each implementation should
        # go to descendant executors
        raise NotImplementedError()

    async def execute_update(self, instance):
        self.connection = await self.db.get_single_connection()
        table = Table(self.model._meta.table)
        query = self.connection.query_class.update(table)
        for field, db_field in self.model._meta.fields_db_projection.items():
            field_object = self.model._meta.fields_map[field]
            if isinstance(field_object, fields.DatetimeField) and field_object.auto_now:
                now = datetime.datetime.utcnow()
                query = query.set(db_field, now)
                setattr(instance, field, now)
            elif field_object.generated:
                continue
            else:
                query = query.set(db_field, getattr(instance, field))
        query = query.where(table.id == instance.id)
        await self.connection.execute_query(str(query))
        await self.db.release_single_connection(self.connection)
        self.connection = None
        return instance

    async def execute_delete(self, instance):
        table = Table(self.model._meta.table)
        query = self.db.query_class.from_(table).where(table.id == instance.id).delete()
        await self.db.execute_query(str(query))
        return instance

    async def _prefetch_reverse_relation(self, instance_list, field, related_query):
        instance_id_set = set()
        for instance in instance_list:
            instance_id_set.add(instance.id)
        backward_relation_manager = getattr(self.model, field)
        relation_field = backward_relation_manager.relation_field

        related_object_list = await related_query.filter(**{
            '{}__in'.format(relation_field): list(instance_id_set)
        })

        related_object_map = {}
        for entry in related_object_list:
            object_id = getattr(entry, relation_field)
            if object_id in related_object_map.keys():
                related_object_map[object_id].append(entry)
            else:
                related_object_map[object_id] = [entry]
        for instance in instance_list:
            relation_container = getattr(instance, field)
            relation_container._set_result_for_query(related_object_map.get(instance.id, []))
        return instance_list

    async def _prefetch_m2m_relation(self, instance_list, field, related_query):
        instance_id_set = set()
        for instance in instance_list:
            instance_id_set.add(instance.id)

        field_object = getattr(self.model, field)

        through_table = Table(field_object.through)

        subquery = self.connection.query_class.from_(through_table).select(
            getattr(through_table, field_object.backward_key).as_('_backward_relation_key'),
            getattr(through_table, field_object.forward_key).as_('_forward_relation_key')
        ).where(getattr(through_table, field_object.backward_key).isin(instance_id_set))

        query = related_query.query.join(subquery).on(
            subquery._forward_relation_key == related_query._table.id
        ).select(
            subquery._backward_relation_key.as_('_backward_relation_key'),
            *[getattr(related_query._table, field).as_(field) for field in related_query.fields]
        )
        raw_results = await self.connection.execute_query(str(query))
        relations = {(e['_backward_relation_key'], e['id']) for e in raw_results}
        related_object_list = [related_query.model(**e) for e in raw_results]
        await self.__class__(
            model=related_query.model,
            db=self.connection,
            prefetch_map=related_query._prefetch_map,
        ).prefetch_for_list(related_object_list)
        related_object_map = {e.id: e for e in related_object_list}
        relation_map = {}

        for object_id, related_object_id in relations:
            if object_id not in relation_map:
                relation_map[object_id] = []
            relation_map[object_id].append(related_object_map[related_object_id])

        for instance in instance_list:
            relation_container = getattr(instance, field)
            relation_container._set_result_for_query(relation_map.get(instance.id, []))
        return instance_list

    async def _prefetch_direct_relation(self, instance_list, field, related_query):
        related_objects_for_fetch = set()
        relation_key_field = '{}_id'.format(field)
        for instance in instance_list:
            if getattr(instance, relation_key_field):
                related_objects_for_fetch.add(getattr(instance, relation_key_field))
        if related_objects_for_fetch:
            related_object_list = await related_query.filter(id__in=list(related_objects_for_fetch))
            related_object_map = {obj.id: obj for obj in related_object_list}
            for instance in instance_list:
                setattr(instance, field, related_object_map.get(getattr(instance, relation_key_field)))
        return instance_list

    def _make_prefetch_queries(self):
        for field, forwarded_prefetches in self.prefetch_map.items():
            related_model_field = self.model._meta.fields_map.get(field)
            related_model = related_model_field.type
            related_query = related_model.all().using_db(self.connection)
            if forwarded_prefetches:
                related_query = related_query.prefetch_related(*forwarded_prefetches)
            self._prefetch_queries[field] = related_query

    async def _do_prefetch(self, instance_id_list, field, related_query):
        if isinstance(getattr(self.model, field), fields.BackwardFKRelation):
            return await self._prefetch_reverse_relation(instance_id_list, field, related_query)
        elif isinstance(getattr(self.model, field), fields.ManyToManyField):
            return await self._prefetch_m2m_relation(instance_id_list, field, related_query)
        else:
            return await self._prefetch_direct_relation(instance_id_list, field, related_query)

    async def _execute_prefetch_queries(self, instance_list):
        if instance_list and self.prefetch_map:
            self._make_prefetch_queries()
            for field, related_query in self._prefetch_queries.items():
                await self._do_prefetch(instance_list, field, related_query)
        return instance_list

    async def prefetch_for_list(self, instance_list, *args):
        self.connection = await self.db.get_single_connection()
        self.prefetch_map = {}
        for relation in args:
            relation_split = relation.split('__')
            first_level_field = relation_split[0]
            assert (
                    first_level_field in self.model._meta.fetch_fields
            ), 'relation {} for {} not found'.format(first_level_field, self.model._meta.table)
            if first_level_field not in self.prefetch_map.keys():
                self.prefetch_map[first_level_field] = set()
            forwarded_prefetch = '__'.join(relation_split[1:])
            if forwarded_prefetch:
                self.prefetch_map[first_level_field].add(forwarded_prefetch)
        await self._execute_prefetch_queries(instance_list)
        await self.db.release_single_connection(self.connection)
        self.connection = None
        return instance_list