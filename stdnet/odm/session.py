import json
from copy import copy
from itertools import chain
from functools import partial

from stdnet import getdb, session_result, session_data
from stdnet.utils.async import on_result, async, multi_async
from stdnet.utils import itervalues, zip
from stdnet.utils.structures import OrderedDict
from stdnet.utils.exceptions import *

from .query import Q, Query, EmptyQuery
from .signals import *


__all__ = ['Session',
           'SessionModel',
           'Manager',
           'LazyProxy',
           'Transaction',
           'commit_when_no_transaction',
           'withsession']

def is_query(query):
    return isinstance(query, Q)


def commit_when_no_transaction(f):
    '''Decorator for committing changes when the instance session is
not in a transaction.'''
    def _(self, *args, **kwargs):
        r = f(self, *args, **kwargs)
        if self.session is not None:
            return self.session.add(self)
        return r
    _.__name__ = f.__name__
    _.__doc__ = f.__doc__
    return _

def withsession(f):
    '''Decorator for instance methods which require a :class:`Session`
available to perform their call. If a session is not available. It raises
a :class:`SessionNotAvailable` exception.'''
    def _(self, *args, **kwargs):
        if self.session is not None:
            return f(self, *args, **kwargs)
        else:
            raise SessionNotAvailable(
                        'Cannot perform operation. No session available')
    _.__name__ = f.__name__
    _.__doc__ = f.__doc__
    return _


class ModelDictionary(dict):
    
    def __contains__(self, model):
        return super(ModelDictionary, self).__contains__(self.meta(model))
    
    def __getitem__(self, model):
        return super(ModelDictionary, self).__getitem__(self.meta(model))
    
    def __setitem__(self, model, value):
        super(ModelDictionary, self).__setitem__(self.meta(model), value)
        
    def get(self, model, default=None):
        return super(ModelDictionary, self).get(self.meta(model), default)
    
    def pop(self, model, *args):
        return super(ModelDictionary, self).pop(self.meta(model), *args)
    
    def meta(self, model):
        return getattr(model, '_meta', model)
        
    
class SessionModel(object):
    '''A :class:`SessionModel` is the container of all objects for a given
:class:`Model` in a stdnet :class:`Session`.'''
    def __init__(self, manager):
        self.manager = manager
        self._new = OrderedDict()
        self._deleted = OrderedDict()
        self._delete_query = []
        self._modified = OrderedDict()
        self._queries = []

    def __len__(self):
        return len(self._new) + len(self._modified) + len(self._deleted)

    def __repr__(self):
        return self._meta.__repr__()
    __str__ = __repr__
    
    def __iter__(self):
        """Iterate over all pending or persistent instances within this
Session model."""
        return iter(self.all())

    def all(self):
        return chain(self._new, self._modified, self._deleted)
    
    @property
    def backend(self):
        '''The backend for this :class:`SessionModel`.'''
        return self.manager.backend
    
    @property
    def read_backend(self):
        '''The read-only backend for this :class:`SessionModel`.'''
        return self.manager.read_backend
    
    @property
    def model(self):
        '''The :class:`Model` for this :class:`SessionModel`.'''
        return self.manager.model
    
    @property
    def _meta(self):
        '''The :class:`Metaclass` for this :class:`SessionModel`.'''
        return self.manager._meta

    @property
    def new(self):
        ''''The set of all new instances within this ``Session``. This instances
will be inserted in the database.'''
        return tuple(itervalues(self._new))

    @property
    def modified(self):
        '''The set of all modified instances within this ``Session``. This
instances will'''
        return tuple(itervalues(self._modified))

    @property
    def deleted(self):
        '''The set of all instances marked as 'deleted' within this
:class:`Session`.'''
        return tuple(itervalues(self._deleted))

    @property
    def dirty(self):
        '''The set of all instances which have changed, but not deleted,
within this :class:`Session`.'''
        return tuple(self.iterdirty())

    def iterdirty(self):
        '''Ordered iterator over dirty elements.'''
        return iter(chain(itervalues(self._new), itervalues(self._modified)))

    def __contains__(self, instance):
        iid = instance.get_state().iid
        return iid in self._new or\
               iid in self._modified or\
               iid in self._deleted

    def get(self, id):
        if id in self._modified:
            return self._modified.get(id)
        elif id in self._deleted:
            return self._deleted.get(id)

    def add(self, instance, modified=True, persistent=None, force_update=False):
        '''Add a new instance to this :class:`SessionModel`.

:param modified: Optional flag indicating if the ``instance`` has been
    modified. By default its value is ``True``.
:param force_update: if ``instance`` is persistent, it forces an update of the
    data rather than a full replacement. This is used by the
    :meth:`insert_update_replace` method. 
:rtype: The instance added to the session'''
        state = instance.get_state()
        if state.deleted:
            raise ValueError('State is deleted. Cannot add.')
        self.pop(state.iid)
        pers = persistent if persistent is not None else state.persistent
        pkname = instance._meta.pkname()
        if not pers:
            instance._dbdata.pop(pkname, None)  # to make sure it is add action
            state = instance.get_state(iid=None)
        elif persistent:
            instance._dbdata[pkname] = instance.pkvalue()
            state = instance.get_state(iid=instance.pkvalue())
        else:
            action = 'update' if force_update else None
            state = instance.get_state(action=action, iid=state.iid)
        iid = state.iid
        if state.persistent:
            if modified:
                self._modified[iid] = instance
        else:
            self._new[iid] = instance
        return instance

    def delete(self, instance, session):
        '''delete an *instance*'''
        inst = self.pop(instance)
        instance = inst if inst is not None else instance
        if instance is not None:
            state = instance.get_state()
            if state.persistent:
                state.deleted = True
                self._deleted[state.iid] = instance
                instance.session = session
            else:
                instance.session = None
            return instance

    def pop(self, instance):
        '''Remove ``instance`` from the :class:`SessionModel`. Instance
could be a :class:`Model` or an id.

:parameter instance: a :class:`Model` or an ``id``.
:rtype: the :class:`Model` removed from session or ``None`` if
    it was not in the session.
'''
        if isinstance(instance, self.model):
            iid = instance.get_state().iid
        else:
            iid = instance
        instance = None
        for d in (self._new, self._modified, self._deleted):
            if iid in d:
                inst = d.pop(iid)
                if instance is None:
                    instance = inst
                elif inst is not instance:
                    raise ValueError('Critical error: %s is duplicated' % iid)
        return instance

    def expunge(self, instance):
        '''Remove *instance* from the :class:`Session`. Instance could be a
:class:`Model` or an id.

:parameter instance: a :class:`Model` or an *id*
:rtype: the :class:`Model` removed from session or ``None`` if
    it was not in the session.
'''
        instance = self.pop(instance)
        instance.session = None
        return instance

    def query(self):
        return self.manager.query(self.model)

    def post_commit(self, results):
        '''\
Process results after a commit.

:parameter results: iterator over :class:`stdnet.instance_session_result`
    items.
:rtype: a two elements tuple containing a list of instances saved and
    a list of ids of instances deleted.'''
        tpy = self._meta.pk_to_python
        instances = []
        deleted = []
        errors = []
        # The length of results must be the same as the length of
        # all committed instances
        for result in results:
            if isinstance(result, Exception):
                errors.append(result.__class__(
                'Exception while commiting {0}. {1}'.format(self.meta, result)))
                continue
            instance = self.pop(result.iid)
            id = tpy(result.id, self.backend)
            if result.deleted:
                deleted.append(id)
            else:
                if instance is None:
                    raise InvalidTransaction('{0} session received id "{1}"\
 which is not in the session.'.format(self, result.iid))
                setattr(instance, instance._meta.pkname(), id)
                instance = self.add(instance,
                                    modified=False,
                                    persistent=result.persistent)
                instance.get_state().score = result.score
                if instance.get_state().persistent:
                    instances.append(instance)
        return instances, deleted, errors

    def flush(self, model):
        '''Completely flush :attr:`model` from the database. No keys
associated with the model will exists after this operation.'''
        return self.backend.flush(self.model)

    def clean(self, model):
        '''Remove empty keys for a :attr:`model` from the database. No
empty keys associated with the model will exists after this operation.'''
        return self.backend.clean(self.model)
        
    def keys(self):
        '''Retrieve all keys for a :attr:`model`. Uses the
:attr:`Manager.read_backend`.'''
        return self.read_backend.model_keys(self.model)
    
    ## INTERNALS
    def get_delete_query(self):
        queries = self._delete_query
        deleted = self.deleted
        if deleted:
            self._deleted.clear()
            if self.model._model_type == 'object':
                q = self.query().filter(id=deleted)
                queries.append(q)
            else:
                queries.extend(deleted)
        if queries:
            self._delete_query = []
            q = queries[0]
            if len(queries) > 1:
                q = q.union(*queries[1:])
            return q
        
    def backends_data(self, transaction):
        be = self.backend
        rbe = self.read_backend
        model= self.model
        meta = model._meta
        dirty = self.dirty
        deletes = self.get_delete_query()
        queries = self._queries
        if dirty or deletes or queries:
            if transaction.signal_delete and deletes:
                pre_delete.send(model, instances=deletes,
                                transaction=transaction)
            if dirty and transaction.signal_commit:
                pre_commit.send(model, instances=dirty,
                                transaction=transaction)
            if be == rbe:
                yield be, session_data(meta, dirty, deletes, queries)
            else:
                if dirty or deletes:
                    yield be, session_data(meta, dirty, deletes, ())
                if queries:
                    yield rbe, session_data(meta, (), (), queries)
            

class SessionStructure(SessionModel):
    '''A :class:`SessionStructure` is the container of all objects for a given
:class:`Structure` in a stdnet :class:`Session`.'''
    def add(self, instance, modified=True, **kwargs):
        state = instance.get_state()
        state.deleted = False
        if not modified:
            self.pop(instance)
        self._modified[state.iid] = instance
        return instance


class Transaction(object):
    '''Transaction class for pipelining commands to the backend server.
An instance of this class is usually obtained via the :meth:`Session.begin`
or the :meth:`Manager.transaction` methods::

    t = session.begin()

or using the ``with`` context manager::

    with session.begin() as t:
        ...

**ATTRIBUTES**

.. attribute:: session

    :class:`Session` which is being transacted.

.. attribute:: name

    Optional :class:`Transaction` name

.. attribute:: backend

    the :class:`stdnet.BackendDataServer` to which the transaction
    is being performed.

.. attribute:: signal_commit

    If ``True``, a signal for each model in the transaction is triggered
    just after changes are committed.
    The signal carries a list of updated ``instances`` of the model,
    the :class:`Session` and the :class:`Transaction` itself.

    default ``True``.

.. attribute:: signal_delete

    If ``True`` a signal for each model in the transaction is triggered
    just after deletion of instances.
    The signal carries the list ``ids`` of deleted instances of the mdoel,
    the :class:`Session` and the :class:`Transaction` itself.

    default ``True``.

.. attribute:: deleted

    Dictionary of list of ids deleted from the backend server after a commit
    operation. This dictionary is only available once the transaction has
    :attr:`finished`.
    
.. attribute:: saved

    Dictionary of list of ids saved in the backend server after a commit
    operation. This dictionary is only available once the transaction has
    :attr:`finished`.
'''
    _finished = False
    def __init__(self, session, name=None, signal_commit=True,
                 signal_delete=True):
        self.name = name or 'transaction'
        self.session = session
        self.on_result = None
        self.signal_commit = signal_commit
        self.signal_delete = signal_delete
        self.deleted = ModelDictionary()
        self.saved = ModelDictionary()

    @property
    def executed(self):
        '''``True`` when this transaction has been executed. A transaction
can be executed once only via the :meth:`commit` method. An executed transaction
if :attr:`finished` once a response from the backend server has been
processed.'''
        return self.session is None
    
    @property
    def finished(self):
        '''``True`` when this transaction is done.'''
        return self._finished

    def add(self, instance, **kwargs):
        '''A convenience proxy for :meth:`Session.add` method.'''
        return self.session.add(instance, **kwargs)

    def delete(self, instance):
        '''A convenience proxy for :meth:`Session.delete` method.'''
        return self.session.delete(instance)
    
    def expunge(self, instance=None):
        '''A convenience proxy for :meth:`Session.expunge` method.'''
        return self.session.expunge(instance)
    
    def query(self, model, **kwargs):
        '''A convenience proxy for :meth:`Session.query` method.'''
        return self.session.query(model, **kwargs)

    def model(self, model):
        '''A convenience proxy for :meth:`Session.model` method.'''
        return self.session.model(model)
    
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        if type is None:
            try:
                return self.commit()
            except:
                self.rollback()
                raise
        else:
            self.rollback()

    def rollback(self):
        if self.session:
            self.session.expunge()
            self.session.transaction = None
            self.session = None

    def commit(self):
        '''Close the transaction and commit session to the backend.'''
        if self.executed:
            raise InvalidTransaction('Invalid operation. '\
                                     'Transaction already executed.')
        self.on_result = self._commit()
        return self.on_result
    
    # INTERNAL FUNCTIONS
    @async()
    def _commit(self):
        session = self.session
        self.session = None
        multi = []
        for backend, data in session.backends_data(self):
            multi.append(backend.execute_session(data))
        resp = yield multi_async(multi)
        if resp:
            yield multi_async((self._post_commit(session, r) for r in resp))
        session.transaction = None
        yield self.finished
        
    @async()
    def _post_commit(self, session, response):
        '''Callback from the :class:`stdnet.BackendDataServer` once the
:attr:`session` commit has finished and results are available.
Results can contain errors.

:parameter response: list of results for each :class:`SessionModel`
    in :attr:`session`. Each element in the list is a three-element tuple
    with the :attr:`SessionModel.meta` element, a list of ids and the commit
    action performed (one of ``save`` and ``delete``).
:parameter commands: The commands executed by the
    :class:`stdnet.BackendDataServer` and stored in this :class:`Transaction`
    for information.'''
        signals = []
        exceptions = []
        for result in response or ():
            if isinstance(result, Exception):
                exceptions.append(result)
            if not isinstance(result, session_result):
                continue
            meta, result = result
            if not result:
                continue
            sm = session.model(meta)
            saved, deleted, errors = sm.post_commit(result)
            exceptions.extend(errors)
            if deleted:
                self.deleted[meta] = deleted
                if self.signal_delete:
                    signals.append((post_delete.send, sm, deleted))
            if saved:
                self.saved[meta] = saved
                if self.signal_commit:
                    signals.append((post_commit.send_robust, sm, saved))
        # Once finished we send signals
        results = []
        for send, sm, instances in signals:
            for _, result in send(sm.model, instances=instances,
                                  session=session, transaction=self):
                results.append(result)
        results = yield multi_async(results, raise_on_error=True)
        self._finished = True
        if exceptions:
            failures = len(exceptions)
            if failures > 1:
                error = 'There were {0} exceptions during commit.\n\n'\
                            .format(failures)
                error += '\n\n'.join((str(e) for e in exceptions))
            else:
                error = str(exceptions[0])
            raise CommitException(error, failures=failures)
        

class Session(object):
    '''The manager of persistent operations on the backend data server for
:class:`StdModel` classes.

.. attribute:: backend

    the :class:`stdnet.BackendDataServer` instance

.. attribute:: transaction

    A :class:`Transaction` instance. Not ``None`` if this :class:`Session`
    is in a :ref:`transactional state <transactional-state>`

.. attribute:: router

    An optional instance of a :class:`Router`. If available, any operation
    on a model is carried out only if that model is in the :attr:`router`
    and it has the same :attr:`backend` as this session.

.. attribute:: query_class

    class for querying. Default is :class:`Query`.
'''
    _structures = {}
    def __init__(self, router, query_class=None):
        self.transaction = None
        self._models = OrderedDict()
        self._router = router
        self.query_class = query_class or Query

    def __str__(self):
        return str(self._router)

    def __repr__(self):
        return '%s: %s' % (self.__class__.__name__, self._router)

    def __iter__(self):
        for sm in self._models.values():
            yield sm

    def __len__(self):
        return len(self._models)
    
    def __getitem__(self, model):
        meta = self.check_model(model)
        if self._router:
            return self._router[meta.model]
        else:
            return Manager(meta.model, self.backend)

    @property
    def router(self):
        return self._router
    
    @property
    def dirty(self):
        '''set of all changed instances in the session'''
        return frozenset(chain(*tuple((sm.dirty for sm\
                                        in itervalues(self._models)))))
        
    def begin(self, **options):
        '''Begin a new :class:`Transaction`. If this :class:`Session`
is already in a :ref:`transactional state <transactional-state>`,
an error will occur. It returns the :attr:`transaction` attribute.

This method is mostly used within a ``with`` statement block::
    
    with session.begin() as t:
        t.add(...)
        ...
        
which is equivalent to::
    
    t = session.begin()
    t.add(...)
    ...
    session.commit()
    
``options`` parameters are passed to the :class:`Transaction` constructor.
'''
        if self.transaction is not None:
            raise InvalidTransaction("A transaction is already begun.")
        else:
            self.transaction = Transaction(self, **options)
        return self.transaction

    def commit(self):
        """Commit the current :attr:`transaction`. If no transaction is in
progress, this method open one. Rarely used directly, see the :meth:`begin`
method for details on how to start and close a transaction using the `with`
construct."""
        if self.transaction is None:
            self.begin()
        return self.transaction.commit()
    
    def query(self, model, query_class=None, **kwargs):
        '''Create a new :class:`Query` for *model*.'''
        query_class = query_class or self.query_class
        sm = self.model(model)
        return query_class(sm._meta, self, **kwargs)

    def empty(self, model):
        '''Returns an empty :class:`Query` for ``model``.'''
        return EmptyQuery(self.manager(model).model, self)

    @async()
    def get_or_create(self, model, **kwargs):
        '''Get an instance of *model* from the internal cache (only if the
dictionary *kwargs* is of length 1 and has key given by ``id``) or from the
server. If it the instance is not available, it tries to create one
from the **kwargs** parameters.

:parameter model: a :class:`StdModel`
:parameter kwargs: dictionary of parameters.
:rtype: an instance of  two elements tuple containing the instance and a boolean
    indicating if the instance was created or not.
'''
        query = self.query(model)
        model = query.model
        items = yield query.filter(**kwargs).all()
        try:
            yield model.get_unique_instance(items), False
        except model.DoesNotExist:
            item = yield self.add(model(**kwargs))
            yield item, True

    def get(self, model, id):
        sm = self._models.get(model._meta)
        if sm:
            return sm.get(id)

    def add(self, instance, modified=True, **params):
        '''Add an *instance* to the session. If the session is not in
a :ref:`transactional state <transactional-state>`, this operation
commits changes to the back-end server immediately and return
what is return by :meth:`Transaction.commit`. Otherwise it return the
input ``instance``.

:parameter instance: a class:`StdModel` or a :class:`Structure` instance.
:parameter modified: a boolean flag indicating if the instance was modified.
:return: the instance.

If the instance is persistent (it is already stored in the database), an updated
will be performed, otherwise a new entry will be created once the :meth:`commit`
method is invoked.
'''
        sm = self.model(instance)
        instance.session = self
        o = sm.add(instance, modified=modified, **params)
        if modified and not self.transaction:
            return on_result(self.commit(), lambda r: o)
        else:
            return o

    def delete(self, instance_or_query):
        '''Include an ``instance`` or a ``query`` to this :class:`Session` list
of data to be deleted. If the session is not in a
:ref:`transactional state <transactional-state>`, this operation commits
changes to the backend server immediately.

:parameter instance_or_query: a :class:`Model` instance or a :class:`Query`.
'''
        sm = self.model(instance_or_query)
        # not an instance of a Model. Assume it is a query.
        if is_query(instance_or_query):
            if instance_or_query.session is not self:
                raise ValueError('Adding a query generated by another session')
            sm._delete_query.append(instance_or_query)
        else:
            instance_or_query = sm.delete(instance_or_query, self)
        return self.commit() if not self.transaction else instance_or_query

    def flush(self, model):
        '''Completely flush a :class:`Model` from the database. No keys
associated with the model will exists after this operation.'''
        return self.model(model).flush()
        return self.backend.flush(self.check_model(model))

    def clean(self, model):
        '''Remove empty keys for a :class:`Model` from the database. No
empty keys associated with the model will exists after this operation.'''
        return self.model(model).clean()
        return self.backend.clean(self.check_model(model))

    def keys(self, model):
        '''Retrieve all keys for a *model*.'''
        return self.model(model).keys()

    def __contains__(self, instance):
        sm = self.model(instance, False)
        return instance in sm if sm is not None else False

    def structure(self, instance):
        '''Return a :class:`stdnet.BackendStructure` for a given
:class:`Structure` *instance*.'''
        return self.backend.structure(instance)

    def model(self, model, create=True):
        '''Returns the :class:`SessionModel` for ``meta``, a class:`Model`
or a :class:`MetaClass`.'''
        manager = self.manager(model) 
        sm = self._models.get(manager)
        if sm is None and create:
            if manager.model._model_type == 'structure':
                sm = SessionStructure(manager)
            else:
                sm = SessionModel(manager)
            self._models[manager] = sm
        return sm

    def expunge(self, instance=None):
        '''Remove ``instance`` from this :class:`Session`. If ``instance``
is not given, it removes all instances from this :class:`Session`.'''
        if instance is not None:
            sm = self._models.get(instance._meta)
            if sm:
                return sm.expunge(instance)
        else:
            self._models.clear()
            
    def manager(self, model):
        try:
            return self.router[model]
        except KeyError:
            meta = getattr(model, '_meta', model)
            raise InvalidTransaction('"%s" not session mapper' % meta)

    def backends_data(self, transaction):
        backends = {}
        for sm in self:
            for backend, data in sm.backends_data(transaction):
                be = backends.get(backend)
                if be is None:
                    backends[backend] = be = []
                be.append(data)
        return backends.items()
            
      
class LazyProxy(object):
    
    def __init__(self, field):
        self.field = field
    
    @property
    def name(self):
        return self.field.name
    
    def load(self, instance, session=None, backend=None):
        raise NotImplementedError
    
    def __get__(self, instance, instance_type=None):
        if not self.field.class_field:
            if instance is None:
                return self
            return self.load(instance, instance.session)
        else:
            return self
    
    
class Manager(object):
    '''A manager class for models. To use a :class:`StdModel` in conjunction
with a :ref:`backend server <db-index>` must be associated with a
:class:`Manager` class. A manager for a model is accessed via a :class:`Router`
which has :ref:`registered the model <tutorial-registration>`::

    class MyModel(odm.StdModel):
        group = odm.SymbolField()
        flag = odm.BooleanField()

    models = odm.Router()
    models.register(MyModel)
    
    manager = models[MyModel]

Managers are used as :class:`Session` and :class:`Query` factories
for a given :class:`StdModel`, but they can be customised::

    session = router[MyModel].session()
    query = router[MyModel].query()
    
To customize a manager for a given model, one creates a subclass and add
additional method::

    class MyModelManager(odm.Manager):
    
        def special_query(self, **kwargs):
            ...
            
At this point we need to tell the model about the custom manager, and we do
so by setting the ``manager_class`` attribute in the :class:`StdModel`::

    class MyModel(odm.StdModel):
        ...
        
        manager_class = MyModelManager
        

.. attribute:: model

    The :class:`StdModel` for this :class:`Manager`. This attribute is
    assigned by the Object data mapper at runtime.

.. attribute:: backend

    The :class:`stdnet.BackendDataServer` for this :class:`Manager`.

.. attribute:: read_backend

    A :class:`stdnet.BackendDataServer` for read-only operations (Queries).

'''
    session_factory = Session
    
    def __init__(self, model, backend=None, read_backend=None, router=None):
        self.model = model
        self._backend = backend
        self._read_backend = read_backend
        self._router = router

    @property
    def _meta(self):
        return self.model._meta
    
    @property
    def backend(self):
        return self._backend
    
    @property
    def read_backend(self):
        return self._read_backend or self._backend
    
    def __getattr__(self, attrname):
        result = getattr(self.model, attrname)
        if isinstance(result, LazyProxy):
            result = result.load(self, backend=self.backend)
            if result.session is None:
                result.session = self.session()
        return result
    
    def __str__(self):
        if self.backend:
            return '{0}({1} - {2})'.format(self.__class__.__name__,
                                           self._meta,
                                           self.backend)
        else:
            return '{0}({1})'.format(self.__class__.__name__, self._meta)
    __repr__ = __str__

    def session(self, session=None):
        '''Returns a new :class:`Session`. This is a shortcut for the
:meth:`Router.session` method.'''
        return self._router.session()
    
    def __call__(self, *args, **kwargs):
        # The callable method is equivalent of doing self.model() it is just
        # a shurtcut for a better API
        return self.model(*args, **kwargs)
    
    def new(self, *args, **kwargs):
        '''Create a new instance of :attr:`model` and commit it to the backend
server. This a shortcut method for the more verbose::
    
    instance = manager.session().add(MyModel(**kwargs))
'''
        return self.session().add(self.model(*args, **kwargs))

    def all(self):
        '''Return all instances for this manager.
Equivalent to::

    self.query().all()
    '''
        return self.query().all()
    
    def create_all(self):
        '''A method which can implement table creation. For sql models. Does
nothing for redis or mongo.'''
        pass        
    
    def query(self, session=None):
        '''Returns a new :class:`Query` for :attr:`Manager.model`.'''
        if session is None:
            session = self.session()
        return session.query(self.model)

    def empty(self):
        '''Returns an empty :class:`Query` for :attr:`Manager.model`.'''
        return self.session().empty(self.model)

    def filter(self, **kwargs):
        '''Returns a new :class:`Query` for :attr:`Manager.model` with
a filter.'''
        return self.query().filter(**kwargs)

    def exclude(self, **kwargs):
        '''Returns a new :class:`Query` for :attr:`Manager.model` with
a exclude filter.'''
        return self.query().exclude(**kwargs)

    def search(self, text, lookup=None):
        '''Returns a new :class:`Query` for :attr:`Manager.model` with
a full text search value.'''
        return self.query().search(text, lookup=lookup)

    def get(self, **kwargs):
        return self.query().get(**kwargs)

    def flush(self):
        return self.session().flush(self.model)

    def clean(self):
        return self.session().clean(self.model)

    def keys(self):
        return self.session().keys(self.model)

    def get_or_create(self, **kwargs):
        return self.session().get_or_create(self.model, **kwargs)

    def pkvalue(self, instance):
        '''Return the primary key value for ``instance``.'''
        return instance.pkvalue()
    
    def __hash__(self):
        return hash(self.model._meta)
