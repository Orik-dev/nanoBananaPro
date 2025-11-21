"""
✅ Очистка БД через ARQ cron с защитой от deadlock
Запускается каждые 10 минут
"""
import logging
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select, delete, and_, func, update, text
from sqlalchemy.exc import OperationalError

from db.engine import SessionLocal
from db.models import Task, Payment

log = logging.getLogger("cleanup_db")


async def _delete_with_retry(session, query_func, max_retries=3):
    """
    ✅ Универсальная функция DELETE с retry для deadlock
    """
    for attempt in range(1, max_retries + 1):
        try:
            result = await session.execute(query_func())
            await session.commit()
            return result.rowcount
        except OperationalError as e:
            await session.rollback()
            error_code = getattr(e.orig, 'args', [None])[0] if hasattr(e, 'orig') else None
            
            # 1213 = Deadlock
            if error_code == 1213:
                if attempt < max_retries:
                    wait_time = 0.5 * attempt
                    log.warning(f"⚠️ Deadlock detected, retry {attempt}/{max_retries} in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    log.error(f"❌ Deadlock after {max_retries} retries")
                    return 0
            else:
                raise
        except Exception:
            await session.rollback()
            raise
    return 0


async def cleanup_database_task(ctx):
    """
    ARQ периодическая задача очистки БД
    Вызывается каждые 10 минут
    """
    log.info("🧹 Starting database cleanup...")
    
    try:
        async with SessionLocal() as session:
            now = datetime.utcnow()
            
            # 1. Удалить completed задачи старше 7 дней (с retry)
            cutoff_completed = now - timedelta(days=7)
            deleted_completed = await _delete_with_retry(
                session,
                lambda: delete(Task).where(and_(
                    Task.status == "completed",
                    Task.created_at < cutoff_completed
                )).execution_options(synchronize_session=False)
            )
            
            # 2. Удалить failed задачи старше 3 дней (с retry)
            cutoff_failed = now - timedelta(days=3)
            deleted_failed = await _delete_with_retry(
                session,
                lambda: delete(Task).where(and_(
                    Task.status == "failed",
                    Task.created_at < cutoff_failed
                )).execution_options(synchronize_session=False)
            )
            
            # 3. Пометить зависшие задачи (>1 час) как failed
            cutoff_stuck = now - timedelta(hours=1)
            try:
                result_stuck = await session.execute(
                    update(Task)
                    .where(and_(
                        Task.status.in_(["queued", "processing"]),
                        Task.created_at < cutoff_stuck
                    ))
                    .values(status="failed")
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                marked_failed = result_stuck.rowcount
            except OperationalError:
                await session.rollback()
                marked_failed = 0
                log.warning("⚠️ Could not mark stuck tasks (deadlock)")
            
            # 4. Удалить pending платежи старше 24 часов
            cutoff_pending = now - timedelta(hours=24)
            deleted_pending = await _delete_with_retry(
                session,
                lambda: delete(Payment).where(and_(
                    Payment.status == "pending",
                    Payment.created_at < cutoff_pending
                )).execution_options(synchronize_session=False)
            )
            
            # 5. Удалить старые completed/cancelled платежи (30 дней)
            cutoff_old_payments = now - timedelta(days=30)
            deleted_old_payments = await _delete_with_retry(
                session,
                lambda: delete(Payment).where(and_(
                    Payment.status.in_(["succeeded", "canceled"]),
                    Payment.created_at < cutoff_old_payments
                )).execution_options(synchronize_session=False)
            )
            
            log.info(
                f"✅ DB Cleanup: "
                f"Tasks(completed:{deleted_completed}, failed:{deleted_failed}, stuck:{marked_failed}), "
                f"Payments(pending:{deleted_pending}, old:{deleted_old_payments})"
            )
            
            # Оптимизация таблиц если удалено много
            total_deleted = deleted_completed + deleted_failed + deleted_pending + deleted_old_payments
            if total_deleted > 100:
                try:
                    # Используем text() для raw SQL
                    await session.execute(text("OPTIMIZE TABLE tasks"))
                    await session.execute(text("OPTIMIZE TABLE payments"))
                    await session.commit()
                    log.info("✅ Tables optimized")
                except Exception as e:
                    log.warning(f"Table optimization skipped: {e}")
            
            # Статистика
            try:
                tasks_total = await session.scalar(select(func.count(Task.id)))
                payments_total = await session.scalar(select(func.count(Payment.id)))
                log.info(f"📊 DB Stats: Tasks={tasks_total}, Payments={payments_total}")
            except Exception:
                pass
    
    except Exception as e:
        log.error(f"❌ DB cleanup error: {e}", exc_info=True)