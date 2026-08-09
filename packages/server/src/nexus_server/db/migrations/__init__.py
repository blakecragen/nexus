"""Placeholder package for Alembic schema migrations.

Current state: **there are no migration revisions yet.** ``alembic`` is declared
as a dependency in ``packages/server/pyproject.toml``, but the running server
creates its schema imperatively in ``nexus_server.main.lifespan()`` via::

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

``create_all`` only ever *adds* missing tables. It will **not** add a new column
to an existing table, change a type, or drop anything. Consequences for anyone
editing ``nexus_server.db.models``:

- Adding a brand-new model/table works on restart with no extra steps.
- Adding a column to an existing model silently does nothing against an
  existing ``nexus.db`` file; queries then fail at runtime with
  ``no such column``. Until Alembic is wired up, the practical options are to
  hand-write the ``ALTER TABLE`` or to recreate the database.

AI Note: this file must stay importable — it is what makes ``migrations`` a
package, so a later ``alembic init`` can drop ``env.py``/``versions/`` in here
without a packaging change. Keep it side-effect free; it is imported implicitly
by any ``nexus_server.db.migrations.*`` import.
"""
