# -*- coding:utf-8 -*-
import time
import warnings
from collections import deque
from contextlib import contextmanager

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS
from django.db.backends import utils
from django.db.backends.signals import connection_created
from django.db.transaction import TransactionManagementError
from django.db.utils import DatabaseError, DatabaseErrorWrapper
from django.utils.functional import cached_property
from django.utils.six.moves import _thread as thread

NO_DB_ALIAS = '__no_db__'


class BaseDatabaseWrapper(object):
    """
    Represents a database connection.
    """
    # Mapping of Field objects to their column types.
    data_types = {}
    # Mapping of Field objects to their SQL suffix such as AUTOINCREMENT.
    data_types_suffix = {}
    # Mapping of Field objects to their SQL for CHECK constraints.
    data_type_check_constraints = {}
    ops = None
    vendor = 'unknown'
    SchemaEditorClass = None

    queries_limit = 9000

    def __init__(self, settings_dict, alias=DEFAULT_DB_ALIAS,
                 allow_thread_sharing=False):
        # Connection related attributes.
        # The underlying database connection.
        self.connection = None
        # `settings_dict` should be a dictionary containing keys such as
        # NAME, USER, etc. It's called `settings_dict` instead of `settings`
        # to disambiguate it from Django settings modules.
        self.settings_dict = settings_dict
        self.alias = alias
        # Query logging in debug mode or when explicitly enabled.
        self.queries_log = deque(maxlen=self.queries_limit)
        self.force_debug_cursor = False

        # Transaction related attributes.
        # Tracks if the connection is in autocommit mode. Per PEP 249, by
        # default, it isn't.
        self.autocommit = False
        # Tracks if the connection is in a transaction managed by 'atomic'.
        self.in_atomic_block = False
        # Increment to generate unique savepoint ids.
        self.savepoint_state = 0
        # List of savepoints created by 'atomic'.
        self.savepoint_ids = []
        # Tracks if the outermost 'atomic' block should commit on exit,
        # ie. if autocommit was active on entry.
        self.commit_on_exit = True
        # Tracks if the transaction should be rolled back to the next
        # available savepoint because of an exception in an inner block.
        self.needs_rollback = False

        # Connection termination related attributes.
        self.close_at = None
        self.closed_in_transaction = False
        self.errors_occurred = False

        # Thread-safety related attributes.
        self.allow_thread_sharing = allow_thread_sharing
        self._thread_ident = thread.get_ident()

    @property
    def queries_logged(self):
        return self.force_debug_cursor or settings.DEBUG

    @property
    def queries(self):
        if len(self.queries_log) == self.queries_log.maxlen:
            warnings.warn(
                "Limit for query logging exceeded, only the last {} queries "
                "will be returned.".format(self.queries_log.maxlen))
        return list(self.queries_log)

    # ##### Backend-specific methods for creating connections and cursors #####

    def get_connection_params(self):
        """Returns a dict of parameters suitable for get_new_connection."""
        raise NotImplementedError('subclasses of BaseDatabaseWrapper may require a get_connection_params() method')

    def get_new_connection(self, conn_params):
        """Opens a connection to the database."""
        raise NotImplementedError('subclasses of BaseDatabaseWrapper may require a get_new_connection() method')

    def init_connection_state(self):
        """Initializes the database connection settings."""
        raise NotImplementedError('subclasses of BaseDatabaseWrapper may require an init_connection_state() method')

    def create_cursor(self):
        """Creates a cursor. Assumes that a connection is established."""
        raise NotImplementedError('subclasses of BaseDatabaseWrapper may require a create_cursor() method')

    # ##### Backend-specific methods for creating connections #####

    def connect(self):
        """Connects to the database. Assumes that the connection is closed."""
        # In case the previous connection was closed while in an atomic block
        self.in_atomic_block = False
        self.savepoint_ids = []
        self.needs_rollback = False

        # Reset parameters defining when to close the connection
        max_age = self.settings_dict['CONN_MAX_AGE']
        self.close_at = None if max_age is None else time.time() + max_age

        self.closed_in_transaction = False
        self.errors_occurred = False

        # Establish the connection
        conn_params = self.get_connection_params()
        self.connection = self.get_new_connection(conn_params)

        # 在创建Connection之后，设置autocommit, 默认为: True
        self.set_autocommit(self.settings_dict['AUTOCOMMIT'])
        self.init_connection_state()

        connection_created.send(sender=self.__class__, connection=self)

    def ensure_connection(self):
        """
        Guarantees that a connection to the database is established.
        """
        if self.connection is None:
            with self.wrap_database_errors:
                self.connect()

    # ##### Backend-specific wrappers for PEP-249 connection methods #####

    def _cursor(self):
        self.ensure_connection()
        with self.wrap_database_errors:
            return self.create_cursor()

    def _commit(self):
        if self.connection is not None:
            with self.wrap_database_errors:
                return self.connection.commit()

    def _rollback(self):
        if self.connection is not None:
            with self.wrap_database_errors:
                return self.connection.rollback()

    def _close(self):
        if self.connection is not None:
            with self.wrap_database_errors:
                return self.connection.close()

    # ##### Generic wrappers for PEP-249 connection methods #####

    def cursor(self):
        """
        Creates a cursor, opening a connection if necessary.
        """
        self.validate_thread_sharing()
        if self.queries_logged:
            cursor = self.make_debug_cursor(self._cursor())
        else:
            cursor = self.make_cursor(self._cursor())
        return cursor

    def commit(self):
        """
        Commits a transaction and resets the dirty flag.
        """
        self.validate_thread_sharing()
        self.validate_no_atomic_block()
        self._commit()
        # A successful commit means that the database connection works.
        self.errors_occurred = False

    def rollback(self):
        """
        Rolls back a transaction and resets the dirty flag.
        """
        self.validate_thread_sharing()
        self.validate_no_atomic_block()
        self._rollback()
        # A successful rollback means that the database connection works.
        self.errors_occurred = False

    def close(self):
        """
        Closes the connection to the database.
        """
        self.validate_thread_sharing()
        # Don't call validate_no_atomic_block() to avoid making it difficult
        # to get rid of a connection in an invalid state. The next connect()
        # will reset the transaction state anyway.
        if self.closed_in_transaction or self.connection is None:
            return
        try:
            self._close()
        finally:
            if self.in_atomic_block:
                self.closed_in_transaction = True
                self.needs_rollback = True
            else:
                self.connection = None

    # ##### Backend-specific savepoint management methods #####

    def _savepoint(self, sid):
        with self.cursor() as cursor:
            cursor.execute(self.ops.savepoint_create_sql(sid))

    def _savepoint_rollback(self, sid):
        with self.cursor() as cursor:
            cursor.execute(self.ops.savepoint_rollback_sql(sid))

    def _savepoint_commit(self, sid):
        with self.cursor() as cursor:
            cursor.execute(self.ops.savepoint_commit_sql(sid))

    def _savepoint_allowed(self):
        # Savepoints cannot be created outside a transaction
        return self.features.uses_savepoints and not self.get_autocommit()

    # ##### Generic savepoint management methods #####

    def savepoint(self):
        """
        Creates a savepoint inside the current transaction. Returns an
        identifier for the savepoint that will be used for the subsequent
        rollback or commit. Does nothing if savepoints are not supported.
        """
        if not self._savepoint_allowed():
            return

        thread_ident = thread.get_ident()
        tid = str(thread_ident).replace('-', '')

        self.savepoint_state += 1
        sid = "s%s_x%d" % (tid, self.savepoint_state)

        self.validate_thread_sharing()
        self._savepoint(sid)

        return sid

    def savepoint_rollback(self, sid):
        """
        Rolls back to a savepoint. Does nothing if savepoints are not supported.
        """
        if not self._savepoint_allowed():
            return

        self.validate_thread_sharing()
        self._savepoint_rollback(sid)

    def savepoint_commit(self, sid):
        """
        Releases a savepoint. Does nothing if savepoints are not supported.
        """
        if not self._savepoint_allowed():
            return

        self.validate_thread_sharing()
        self._savepoint_commit(sid)

    def clean_savepoints(self):
        """
        Resets the counter used to generate unique savepoint ids in this thread.
        """
        self.savepoint_state = 0

    # ##### Backend-specific transaction management methods #####

    def _set_autocommit(self, autocommit):
        """
        Backend-specific implementation to enable or disable autocommit.
        """
        raise NotImplementedError('subclasses of BaseDatabaseWrapper may require a _set_autocommit() method')

    # ##### Generic transaction management methods #####

    def get_autocommit(self):
        """
        Check the autocommit state.
        """
        self.ensure_connection()
        return self.autocommit

    def set_autocommit(self, autocommit):
        """
        Enable or disable autocommit.
        """
        self.validate_no_atomic_block()
        self.ensure_connection()
        self._set_autocommit(autocommit)
        self.autocommit = autocommit

    def get_rollback(self):
        """
        Get the "needs rollback" flag -- for *advanced use* only.
        """
        if not self.in_atomic_block:
            raise TransactionManagementError(
                "The rollback flag doesn't work outside of an 'atomic' block.")
        return self.needs_rollback

    def set_rollback(self, rollback):
        """
        Set or unset the "needs rollback" flag -- for *advanced use* only.
        """
        if not self.in_atomic_block:
            raise TransactionManagementError(
                "The rollback flag doesn't work outside of an 'atomic' block.")
        self.needs_rollback = rollback

    def validate_no_atomic_block(self):
        """
        Raise an error if an atomic block is active.
        确保当前的代码现在不在事务之中
        """
        if self.in_atomic_block:
            raise TransactionManagementError(
                "This is forbidden when an 'atomic' block is active.")

    def validate_no_broken_transaction(self):
        if self.needs_rollback:
            raise TransactionManagementError(
                "An error occurred in the current transaction. You can't "
                "execute queries until the end of the 'atomic' block.")

    # ##### Foreign key constraints checks handling #####

    @contextmanager
    def constraint_checks_disabled(self):
        """
        Context manager that disables foreign key constraint checking.
        """
        disabled = self.disable_constraint_checking()
        try:
            yield
        finally:
            if disabled:
                self.enable_constraint_checking()

    def disable_constraint_checking(self):
        """
        Backends can implement as needed to temporarily disable foreign key
        constraint checking. Should return True if the constraints were
        disabled and will need to be reenabled.
        """
        return False

    def enable_constraint_checking(self):
        """
        Backends can implement as needed to re-enable foreign key constraint
        checking.
        """
        pass

    def check_constraints(self, table_names=None):
        """
        Backends can override this method if they can apply constraint
        checking (e.g. via "SET CONSTRAINTS ALL IMMEDIATE"). Should raise an
        IntegrityError if any invalid foreign key references are encountered.
        """
        pass

    # ##### Connection termination handling #####

    def is_usable(self):
        """
        Tests if the database connection is usable.

        This function may assume that self.connection is not None.

        Actual implementations should take care not to raise exceptions
        as that may prevent Django from recycling unusable connections.
        """
        raise NotImplementedError(
            "subclasses of BaseDatabaseWrapper may require an is_usable() method")

    def close_if_unusable_or_obsolete(self):
        """
        Closes the current connection if unrecoverable errors have occurred,
        or if it outlived its maximum age.
        """
        if self.connection is not None:
            # 1. 保持默认的 AUTOCOMMIT, 如果当前状态和 配置不一样，关闭connection
            # If the application didn't restore the original autocommit setting,
            # don't take chances, drop the connection.
            if self.get_autocommit() != self.settings_dict['AUTOCOMMIT']:
                self.close()
                return

            # If an exception other than DataError or IntegrityError occurred
            # since the last commit / rollback, check if the connection works.
            if self.errors_occurred:
                if self.is_usable():
                    self.errors_occurred = False
                else:
                    self.close()
                    return

            # 2. 可以防止connection长时间没有访问，过期
            #    通过max-age来控制close_at
            if self.close_at is not None and time.time() >= self.close_at:
                self.close()
                return

    # ##### Thread safety handling #####

    def validate_thread_sharing(self):
        """
        Validates that the connection isn't accessed by another thread than the
        one which originally created it, unless the connection was explicitly
        authorized to be shared between threads (via the `allow_thread_sharing`
        property). Raises an exception if the validation fails.
        """
        if not (self.allow_thread_sharing
                or self._thread_ident == thread.get_ident()):
            raise DatabaseError("DatabaseWrapper objects created in a "
                "thread can only be used in that same thread. The object "
                "with alias '%s' was created in thread id %s and this is "
                "thread id %s."
                % (self.alias, self._thread_ident, thread.get_ident()))

    # ##### Miscellaneous #####

    def prepare_database(self):
        """
        Hook to do any database check or preparation, generally called before
        migrating a project or an app.
        """
        pass

    @cached_property
    def wrap_database_errors(self):
        """
        Context manager and decorator that re-throws backend-specific database
        exceptions using Django's common wrappers.
        """
        return DatabaseErrorWrapper(self)

    def make_debug_cursor(self, cursor):
        """
        Creates a cursor that logs all queries in self.queries_log.
        """
        return utils.CursorDebugWrapper(cursor, self)

    def make_cursor(self, cursor):
        """
        Creates a cursor without debug logging.
        """
        return utils.CursorWrapper(cursor, self)

    @contextmanager
    def temporary_connection(self):
        """
        Context manager that ensures that a connection is established, and
        if it opened one, closes it to avoid leaving a dangling connection.
        This is useful for operations outside of the request-response cycle.

        Provides a cursor: with self.temporary_connection() as cursor: ...
        """
        must_close = self.connection is None
        cursor = self.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
            if must_close:
                self.close()

    @cached_property
    def _nodb_connection(self):
        """
        Alternative connection to be used when there is no need to access
        the main database, specifically for test db creation/deletion.
        This also prevents the production database from being exposed to
        potential child threads while (or after) the test database is destroyed.
        Refs #10868, #17786, #16969.
        """
        settings_dict = self.settings_dict.copy()
        settings_dict['NAME'] = None
        nodb_connection = self.__class__(
            settings_dict,
            alias=NO_DB_ALIAS,
            allow_thread_sharing=False)
        return nodb_connection

    def _start_transaction_under_autocommit(self):
        """
        Only required when autocommits_when_autocommit_is_off = True.
        """
        raise NotImplementedError(
            'subclasses of BaseDatabaseWrapper may require a '
            '_start_transaction_under_autocommit() method'
        )

    def schema_editor(self, *args, **kwargs):
        """
        Returns a new instance of this backend's SchemaEditor.
        """
        if self.SchemaEditorClass is None:
            raise NotImplementedError(
                'The SchemaEditorClass attribute of this database wrapper is still None')
        return self.SchemaEditorClass(self, *args, **kwargs)
