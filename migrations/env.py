from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.models import Base

target_metadata = Base.metadata


def include_name(name, type_, parent_names) -> bool:
    # Procrastinate's queue tables are created by its own SchemaManager (see the foundation migration),
    # so autogenerate must not try to drop them.
    return not (type_ == "table" and name is not None and name.startswith("procrastinate_"))


def run_migrations() -> None:
    # Tests pass their own connection via config.attributes.
    connection = context.config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
