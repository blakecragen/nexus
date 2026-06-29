"""Repository pattern — public database operations API.

All callers import from here. Internal models and session management are
implementation details that can be swapped without breaking consumers.

Following the HVE-Automation-Worker pattern: ops.py is the ONLY stable
public interface to the database.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_server.db.models import (
    Artifact,
    AuditLog,
    Credential,
    Group,
    GroupPoolAccess,
    Job,
    Node,
    Pool,
    PoolNodeMembership,
    SavedTemplate,
    StepRun,
    StorageBackend,
    StorageTransfer,
    User,
    UserGroupMembership,
)


def _sid(val) -> str | None:
    """Coerce a UUID or string to a plain string for SQLite compatibility."""
    return str(val) if val is not None else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Users ───────────────────────────────────────────────────────────────


async def create_user(
    db: AsyncSession, username: str, password_hash: str, email: str | None = None,
    role: str = "user",
) -> User:
    user = User(
        username=username, password_hash=password_hash, email=email,
        role=role, api_key=secrets.token_urlsafe(32),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    return await db.get(User, _sid(user_id))


async def get_user_by_api_key(db: AsyncSession, api_key: str) -> User | None:
    result = await db.execute(select(User).where(User.api_key == api_key))
    return result.scalar_one_or_none()


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.username))
    return list(result.scalars().all())


async def update_user(db: AsyncSession, user_id: UUID, **kwargs: Any) -> User | None:
    user = await db.get(User, user_id)
    if not user:
        return None
    for k, v in kwargs.items():
        setattr(user, k, v)
    await db.commit()
    await db.refresh(user)
    return user


# ── Groups ──────────────────────────────────────────────────────────────


async def create_group(
    db: AsyncSession, name: str, created_by: UUID, description: str | None = None,
) -> Group:
    group = Group(name=name, description=description, created_by=created_by)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def list_groups(db: AsyncSession) -> list[Group]:
    result = await db.execute(select(Group).order_by(Group.name))
    return list(result.scalars().all())


async def add_user_to_group(
    db: AsyncSession, user_id: UUID, group_id: UUID, role_in_group: str = "member",
) -> UserGroupMembership:
    membership = UserGroupMembership(
        user_id=user_id, group_id=group_id, role_in_group=role_in_group,
    )
    db.add(membership)
    await db.commit()
    return membership


async def remove_user_from_group(db: AsyncSession, user_id: UUID, group_id: UUID) -> None:
    await db.execute(
        delete(UserGroupMembership).where(
            UserGroupMembership.user_id == _sid(user_id),
            UserGroupMembership.group_id == _sid(group_id),
        )
    )
    await db.commit()


async def set_group_pool_access(
    db: AsyncSession, group_id: UUID, pool_id: UUID, permission: str = "submit",
) -> GroupPoolAccess:
    # Upsert
    existing = await db.execute(
        select(GroupPoolAccess).where(
            GroupPoolAccess.group_id == _sid(group_id),
            GroupPoolAccess.pool_id == _sid(pool_id),
        )
    )
    access = existing.scalar_one_or_none()
    if access:
        access.permission = permission
    else:
        access = GroupPoolAccess(group_id=group_id, pool_id=pool_id, permission=permission)
        db.add(access)
    await db.commit()
    return access


async def check_user_pool_access(db: AsyncSession, user_id: UUID, pool_id: UUID) -> bool:
    """Check if user has access to a pool (admin bypasses, otherwise checks group membership)."""
    user = await db.get(User, user_id)
    if not user:
        return False
    if user.role == "admin":
        return True
    result = await db.execute(
        select(GroupPoolAccess)
        .join(UserGroupMembership, UserGroupMembership.group_id == GroupPoolAccess.group_id)
        .where(
            UserGroupMembership.user_id == _sid(user_id),
            GroupPoolAccess.pool_id == _sid(pool_id),
        )
    )
    return result.first() is not None


# ── Nodes ───────────────────────────────────────────────────────────────


async def create_node(db: AsyncSession, **kwargs: Any) -> Node:
    node = Node(api_key=secrets.token_urlsafe(32), **kwargs)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


async def get_node_by_id(db: AsyncSession, node_id: UUID) -> Node | None:
    return await db.get(Node, _sid(node_id))


async def get_node_by_api_key(db: AsyncSession, api_key: str) -> Node | None:
    result = await db.execute(select(Node).where(Node.api_key == api_key))
    return result.scalar_one_or_none()


async def list_nodes(
    db: AsyncSession,
    os_type: str | None = None,
    status: str | None = None,
    pool_id: UUID | None = None,
) -> list[Node]:
    query = select(Node)
    if os_type:
        query = query.where(Node.os_type == os_type)
    if status:
        query = query.where(Node.status == status)
    if pool_id:
        query = query.join(PoolNodeMembership).where(PoolNodeMembership.pool_id == _sid(pool_id))
    query = query.order_by(Node.hostname)
    result = await db.execute(query)
    return list(result.scalars().all())


async def update_node(db: AsyncSession, node_id: UUID, **kwargs: Any) -> Node | None:
    node = await db.get(Node, _sid(node_id))
    if not node:
        return None
    for k, v in kwargs.items():
        setattr(node, k, v)
    await db.commit()
    await db.refresh(node)
    return node


async def delete_node(db: AsyncSession, node_id: UUID) -> bool:
    node = await db.get(Node, _sid(node_id))
    if not node:
        return False
    await db.delete(node)
    await db.commit()
    return True


# ── Pools ───────────────────────────────────────────────────────────────


async def create_pool(
    db: AsyncSession, name: str, created_by: UUID, description: str | None = None,
) -> Pool:
    pool = Pool(name=name, description=description, created_by=created_by)
    db.add(pool)
    await db.commit()
    await db.refresh(pool)
    return pool


async def get_pool_by_id(db: AsyncSession, pool_id: UUID) -> Pool | None:
    return await db.get(Pool, _sid(pool_id))


async def list_pools(db: AsyncSession) -> list[Pool]:
    result = await db.execute(select(Pool).order_by(Pool.name))
    return list(result.scalars().all())


async def add_node_to_pool(db: AsyncSession, pool_id: UUID, node_id: UUID) -> PoolNodeMembership:
    membership = PoolNodeMembership(pool_id=pool_id, node_id=node_id)
    db.add(membership)
    await db.commit()
    return membership


async def remove_node_from_pool(db: AsyncSession, pool_id: UUID, node_id: UUID) -> None:
    await db.execute(
        delete(PoolNodeMembership).where(
            PoolNodeMembership.pool_id == _sid(pool_id),
            PoolNodeMembership.node_id == _sid(node_id),
        )
    )
    await db.commit()


async def get_pool_nodes(db: AsyncSession, pool_id: UUID) -> list[Node]:
    result = await db.execute(
        select(Node).join(PoolNodeMembership).where(PoolNodeMembership.pool_id == _sid(pool_id))
    )
    return list(result.scalars().all())


# ── Jobs ────────────────────────────────────────────────────────────────


async def create_job(
    db: AsyncSession, name: str, submitted_by: UUID, steps_config: list[dict],
    target_pool_id: UUID | None = None, target_node_id: UUID | None = None,
    priority: int = 1, storage_target: str | None = None,
) -> Job:
    job = Job(
        name=name, submitted_by=_sid(submitted_by), steps_config=steps_config,
        target_pool_id=_sid(target_pool_id), target_node_id=_sid(target_node_id),
        priority=priority, storage_target=storage_target,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job_by_id(db: AsyncSession, job_id: UUID) -> Job | None:
    return await db.get(Job, _sid(job_id))


async def list_jobs(
    db: AsyncSession,
    status: str | None = None,
    submitted_by: UUID | None = None,
    pool_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Job]:
    query = select(Job)
    if status:
        query = query.where(Job.status == status)
    if submitted_by:
        query = query.where(Job.submitted_by == submitted_by)
    if pool_id:
        query = query.where(Job.target_pool_id == _sid(pool_id))
    query = query.order_by(Job.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


async def update_job(db: AsyncSession, job_id: UUID, **kwargs: Any) -> Job | None:
    job = await db.get(Job, _sid(job_id))
    if not job:
        return None
    for k, v in kwargs.items():
        setattr(job, k, v)
    await db.commit()
    await db.refresh(job)
    return job


async def append_job_log(db: AsyncSession, job_id: UUID, text: str) -> None:
    """Append a block of text to a job's aggregated terminal log. Committed
    incrementally so a crash mid-job still leaves a partial log."""
    job = await db.get(Job, _sid(job_id))
    if job:
        job.log_text = (job.log_text or "") + text
        await db.commit()


async def get_active_jobs(db: AsyncSession) -> list[Job]:
    result = await db.execute(
        select(Job).where(Job.status.in_(["pending", "queued", "running"]))
    )
    return list(result.scalars().all())# ── Step Runs ───────────────────────────────────────────────────────────


async def create_step_run(
    db: AsyncSession, job_id: UUID, step_index: int, step_name: str,
    input_params: dict | None = None,
) -> StepRun:
    step_run = StepRun(
        job_id=job_id, step_index=step_index, step_name=step_name,
        input_params=input_params,
    )
    db.add(step_run)
    await db.commit()
    await db.refresh(step_run)
    return step_run


async def update_step_run(db: AsyncSession, step_run_id: UUID, **kwargs: Any) -> StepRun | None:
    step_run = await db.get(StepRun, _sid(step_run_id))
    if not step_run:
        return None
    for k, v in kwargs.items():
        setattr(step_run, k, v)
    await db.commit()
    await db.refresh(step_run)
    return step_run


async def get_step_runs_for_job(db: AsyncSession, job_id: UUID) -> list[StepRun]:
    result = await db.execute(
        select(StepRun).where(StepRun.job_id == _sid(job_id)).order_by(StepRun.step_index)
    )
    return list(result.scalars().all())


async def get_latest_step_run(
    db: AsyncSession, job_id: UUID, step_index: int,
) -> StepRun | None:
    """Return the most recently created step_run for (job_id, step_index).

    Loops produce multiple step_runs at the same step_index; this picks the
    one currently in flight.
    """
    result = await db.execute(
        select(StepRun)
        .where(StepRun.job_id == _sid(job_id), StepRun.step_index == step_index)
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Credentials ─────────────────────────────────────────────────────────


async def create_credential(
    db: AsyncSession, name: str, credential_type: str, encrypted_fields: bytes,
    owner_id: UUID, is_shared: bool = False, allowed_groups: list | None = None,
    description: str | None = None,
) -> Credential:
    cred = Credential(
        name=name, credential_type=credential_type, encrypted_fields=encrypted_fields,
        owner_id=owner_id, is_shared=is_shared,
        allowed_groups=allowed_groups or [], description=description,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return cred


async def get_credential_by_id(db: AsyncSession, cred_id: UUID) -> Credential | None:
    return await db.get(Credential, _sid(cred_id))


async def get_credential_by_name(db: AsyncSession, name: str) -> Credential | None:
    result = await db.execute(select(Credential).where(Credential.name == name))
    return result.scalar_one_or_none()


async def list_credentials(db: AsyncSession) -> list[Credential]:
    result = await db.execute(select(Credential).order_by(Credential.name))
    return list(result.scalars().all())


async def update_credential(db: AsyncSession, cred_id: UUID, **kwargs: Any) -> Credential | None:
    cred = await db.get(Credential, cred_id)
    if not cred:
        return None
    kwargs["updated_at"] = _utcnow()
    for k, v in kwargs.items():
        setattr(cred, k, v)
    await db.commit()
    await db.refresh(cred)
    return cred


async def delete_credential(db: AsyncSession, cred_id: UUID) -> bool:
    cred = await db.get(Credential, cred_id)
    if not cred:
        return False
    await db.delete(cred)
    await db.commit()
    return True


# ── Storage Backends ────────────────────────────────────────────────────


async def create_storage_backend(db: AsyncSession, **kwargs: Any) -> StorageBackend:
    backend = StorageBackend(**kwargs)
    db.add(backend)
    await db.commit()
    await db.refresh(backend)
    return backend


async def get_storage_backend_by_id(db: AsyncSession, backend_id: UUID) -> StorageBackend | None:
    return await db.get(StorageBackend, _sid(backend_id))


async def get_default_storage_backend(db: AsyncSession) -> StorageBackend | None:
    result = await db.execute(
        select(StorageBackend).where(
            StorageBackend.is_default == True, StorageBackend.is_active == True
        )
    )
    return result.scalar_one_or_none()


async def list_storage_backends(db: AsyncSession) -> list[StorageBackend]:
    result = await db.execute(
        select(StorageBackend).order_by(StorageBackend.priority, StorageBackend.name)
    )
    return list(result.scalars().all())


# ── Artifacts ───────────────────────────────────────────────────────────


async def create_artifact(db: AsyncSession, **kwargs: Any) -> Artifact:
    artifact = Artifact(**kwargs)
    db.add(artifact)
    await db.commit()
    await db.refresh(artifact)
    return artifact


async def list_artifacts_for_job(db: AsyncSession, job_id: UUID) -> list[Artifact]:
    result = await db.execute(
        select(Artifact).where(Artifact.job_id == _sid(job_id)).order_by(Artifact.created_at)
    )
    return list(result.scalars().all())


async def get_artifact_by_id(db: AsyncSession, artifact_id: UUID) -> Artifact | None:
    return await db.get(Artifact, _sid(artifact_id))


# ── Storage Transfers ───────────────────────────────────────────────────


async def create_transfer(db: AsyncSession, **kwargs: Any) -> StorageTransfer:
    transfer = StorageTransfer(**kwargs)
    db.add(transfer)
    await db.commit()
    await db.refresh(transfer)
    return transfer


async def update_transfer(db: AsyncSession, transfer_id: UUID, **kwargs: Any) -> StorageTransfer | None:
    transfer = await db.get(StorageTransfer, _sid(transfer_id))
    if not transfer:
        return None
    for k, v in kwargs.items():
        setattr(transfer, k, v)
    await db.commit()
    await db.refresh(transfer)
    return transfer


async def list_transfers(db: AsyncSession, status: str | None = None) -> list[StorageTransfer]:
    query = select(StorageTransfer)
    if status:
        query = query.where(StorageTransfer.status == status)
    query = query.order_by(StorageTransfer.started_at.desc().nullslast())
    result = await db.execute(query)
    return list(result.scalars().all())


# ── Saved Templates ────────────────────────────────────────────────────


async def create_template(
    db: AsyncSession, name: str, steps_config: list[dict], created_by: UUID,
    description: str | None = None,
) -> SavedTemplate:
    template = SavedTemplate(
        name=name, steps_config=steps_config, created_by=created_by,
        description=description,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


async def list_templates(db: AsyncSession) -> list[SavedTemplate]:
    result = await db.execute(select(SavedTemplate).order_by(SavedTemplate.name))
    return list(result.scalars().all())


async def delete_template(db: AsyncSession, template_id: UUID) -> bool:
    template = await db.get(SavedTemplate, _sid(template_id))
    if not template:
        return False
    await db.delete(template)
    await db.commit()
    return True


# ── Audit Log ───────────────────────────────────────────────────────────


async def create_audit_entry(
    db: AsyncSession, action: str, user_id: UUID | None = None,
    target_type: str | None = None, target_id: UUID | None = None,
    details: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user_id, action=action, target_type=target_type,
        target_id=target_id, details=details,
    )
    db.add(entry)
    await db.commit()
    return entry
