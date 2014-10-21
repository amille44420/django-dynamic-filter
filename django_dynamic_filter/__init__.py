from collections import OrderedDict
import copy

from django.utils import six
from django.db.models.base import ModelBase
from django.forms import Field as FormField, Form


def check_field_options(new_class, opts):
    # common helper to check errors in field metadata  (used with Field & ModelField)
    if not opts.field:
        raise ValueError('{model}.Meta.field have to be specified.'.format(model=new_class.__name__))

    # then a field have to be attached to later render it in a form
    if not isinstance(opts.field, FormField):
        raise TypeError('{model}.Meta.field have to a valid Django Field, cannot be a {{type}}'.format(
            model=new_class.__name__, type=opts.field.__name__))


class FieldOptions(object):
    # options for Field
    def __init__(self, options=None):
        self.field = getattr(options, 'field', None)
        self.operator = getattr(options, 'operator', None)  # optional
        self.force_empty = getattr(options, 'force_empty', False)  # optional
        self.name = getattr(options, 'name', None)  # optional

    def __deepcopy__(self, memo):
        result = copy.copy(self)
        memo[id(self)] = result
        result.field = copy.deepcopy(self.field, memo)
        return result


class FieldMetaClass(type):
    # meta class for Field
    def __new__(mcs, name, bases, attrs):
        # nothing to do before, so we render the new class
        new_class = super(FieldMetaClass, mcs).__new__(mcs, name, bases, attrs)

        if bases == (BaseField,):
            return new_class

        # then get meta data (also called options)
        opts = new_class._meta = FieldOptions(options=getattr(new_class, 'Meta', None))
        check_field_options(new_class, opts)  # check options

        return new_class  # done


class BaseField(object):
    # base field
    def render_operator(self):
        # hook to render the operator string
        return self._meta.name if not self._meta.operator else '{field}__{operator}'.format(field=self._meta.name,
                                                                                            operator=self._meta.operator)

    def render_value(self, value):
        # hook to set the value to the query
        return value

    def store(self, value):
        # hook to set the value to be stored in sessions
        return value

    def unstore(self, value):
        # hook to set the value to be unstored from he sessions
        return value

    def __deepcopy__(self, memo):
        result = copy.copy(self)
        memo[id(self)] = result
        result._meta = copy.deepcopy(self._meta, memo)
        return result


class Field(six.with_metaclass(FieldMetaClass, BaseField)):
    # basic Field (class to extend)
    pass


class ModelFieldOptions(FieldOptions):
    # Options available for ModelField
    def __init__(self, options=None):
        super(ModelFieldOptions, self).__init__(options=options)
        self.queryset = getattr(options, 'queryset', None)


class ModelFieldMetaClass(FieldMetaClass):
    # meta class for ModelField
    def __new__(mcs, name, bases, attrs):
        # nothing to do before, so we render the new class
        new_class = super(ModelFieldMetaClass, mcs).__new__(mcs, name, bases, attrs)

        if bases == (BaseField,):
            return new_class

        # then get meta data (also called options)
        opts = new_class._meta = ModelFieldOptions(options=getattr(new_class, 'Meta', None))
        check_field_options(new_class, opts)  # check options

        # check queryset
        if not opts.queryset:
            raise ValueError('Queryset has to be specified.')

        return new_class  # done


class ModelField(six.with_metaclass(ModelFieldMetaClass, BaseField)):
    # ModelField allow to store an object only in session by saving its primary key
    def store(self, value):
        return value.pk if value else None

    def unstore(self, value):
        return self._meta.queryset.get(pk=value) if value else None


class DynamicFilterOptions(object):
    # options for DynamicFilter
    def __init__(self, options=None):
        self.model = getattr(options, 'model', None)


class DynamicFilterMetaClass(type):
    # meta class for DynamicFilter
    def __new__(mcs, name, bases, attrs):
        # Collect fields from current class
        current_fields = []
        for key, value in list(attrs.items()):
            if isinstance(value, BaseField):
                if not value._meta.name:
                    value._meta.name = key
                current_fields.append((key, value))
                attrs.pop(key)
        current_fields.sort(key=lambda x: x[1]._meta.field.creation_counter)
        current_fields = OrderedDict(current_fields)  # we'll use them later to build the form

        # now we render the new class
        new_class = super(DynamicFilterMetaClass, mcs).__new__(mcs, name, bases, attrs)

        if bases == (BaseDynamicFilter,):
            return new_class

        # get meta class (also called options)
        opts = new_class._meta = DynamicFilterOptions(getattr(new_class, 'Meta', None))

        # raise error about model option
        if not opts.model:  # model not specified
            raise ValueError('{model}.Meta.model cannot be a null.'.format(model=new_class.__name__))
        if not isinstance(opts.model, ModelBase):  # invalid type
            raise TypeError('{model}.Meta.model have to be a valid Django Model, cannot be a {type}'.format(
                model=new_class.__name__, type=opts.model.__name__))

        new_class._meta.base_fields = current_fields  # we save current fields as base fields
        new_class.name = name  # primary name use as identifier key

        return new_class  # done


class BaseDynamicFilter(object):
    # base for DynamicFilter
    def __init__(self, request):
        self.request = request

        # check sessions (for first init)
        self.first_init = True if not self.request.session.__contains__(self.name) else False
        self.is_reset = True if self.request.GET.get('reset_filter', None) == self.name else False
        if self.first_init or self.is_reset:
            self.values = {}  # first init
            if not self.first_init and self.is_reset:
                del self.request.session[self.name]
        else:
            self.values = self.request.session.get(self.name, {})

        # using deepcopy we're going to make unique the fields set for this instance
        self.fields = copy.deepcopy(self._meta.base_fields)

        # we list form fields
        form_fields = {}
        for name, field in self.fields.items():
            form_field = field._meta.field
            setattr(form_field, 'initial', self.get_value(name, getattr(form_field, 'initial', None), init=True))
            setattr(form_field, 'required', False)
            form_fields[name] = form_field  # get form field

        # now everything is right, we're gonna build the form
        base_classes = (Form,)  # basis
        form_type = type('DynamicFilterForm', base_classes, form_fields)  # build type
        if self.request.method == 'POST' and not self.is_reset:
            self.form = form_type(data=self.request.POST)  # build form with previous data
            if self.form.is_valid():
                for name, value in self.form.cleaned_data.items():
                    self.set_value(name, value)
        else:
            self.form = form_type()  # build form instance without data

        # up to date session
        self.request.session[self.name] = self.values
        self.request.session.modified = True

    def get_value(self, name, default=None, init=False):
        # get value from sessions
        try:
            return self.fields[name].unstore(self.values[name])  # unpack it and return it
        except KeyError:
            if init:  # is the value is not existing yet in session and init is asked, store default value
                self.set_value(name, default)
            return default

    def set_value(self, name, value):
        self.values[name] = self.fields[name].store(value)

    def render_query_kwargs(self):
        kwargs = {}  # empty kwargs

        for name, field in self.fields.items():  # check every field
            value = field.render_value(self.get_value(name))  # get current value
            if value or field._meta.force_empty:  # force_empty allow empty values to be used in
                kwargs[field.render_operator()] = value

        return kwargs  # final kwargs list

    def render_query(self, *args, **kwargs):
        # you could passe additional argument to specify the queryset
        filter_kwargs = self.render_query_kwargs()
        queryset = self._meta.model.objects
        if kwargs:
            queryset = queryset.filter(**kwargs)
        if not filter_kwargs:
            return queryset  # we don't need to filter something
        return queryset.filter(**filter_kwargs)  # pass kwargs to filter method

    def is_active(self):
        # define if the filter is active or not
        return True if self.render_query_kwargs() else False


class DynamicFilter(six.with_metaclass(DynamicFilterMetaClass, BaseDynamicFilter)):
    # DynamicFilter (class to extend)
    pass
