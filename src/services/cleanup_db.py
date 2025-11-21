# """
# ✅ Очистка БД через ARQ cron с защитой от deadlock
# Запускается каждые 10 минут
# """
# import logging
# import asyncio
# from datetime import datetime, timedelta
# from sqlalchemy import select, delete, and_, func, update, text
# from sqlalchemy.exc import OperationalError

# from db.engine import SessionLocal
# from db.models import Task, Payment

# log = logging.getLogger("cleanup_db")


# async def _delete_with_retry(session, query_func, max_retries=3):
#     """
#     ✅ Универсальная функция DELETE с retry для deadlock
#     """
#     for attempt in range(1, max_retries + 1):
#         try:
#             result = await session.execute(query_func())
#             await session.commit()
#             return result.rowcount
#         except OperationalError as e:
#             await session.rollback()
#             error_code = getattr(e.orig, 'args', [None])[0] if hasattr(e, 'orig') else None
            
#             # 1213 = Deadlock
#             if error_code == 1213:
#                 if attempt < max_retries:
#                     wait_time = 0.5 * attempt
#                     log.warning(f"⚠️ Deadlock detected, retry {attempt}/{max_retries} in {wait_time}s")
#                     await asyncio.sleep(wait_time)
#                     continue
#                 else:
#                     log.error(f"❌ Deadlock after {max_retries} retries")
#                     return 0
#             else:
#                 raise
#         except Exception:
#             await session.rollback()
#             raise
#     return 0


# async def cleanup_database_task(ctx):
#     """
#     ARQ периодическая задача очистки БД
#     Вызывается каждые 10 минут
#     """
#     log.info("🧹 Starting database cleanup...")
    
#     try:
#         async with SessionLocal() as session:
#             now = datetime.utcnow()
            
#             # 1. Удалить completed задачи старше 7 дней (с retry)
#             cutoff_completed = now - timedelta(days=7)
#             deleted_completed = await _delete_with_retry(
#                 session,
#                 lambda: delete(Task).where(and_(
#                     Task.status == "completed",
#                     Task.created_at < cutoff_completed
#                 )).execution_options(synchronize_session=False)
#             )
            
#             # 2. Удалить failed задачи старше 3 дней (с retry)
#             cutoff_failed = now - timedelta(days=3)
#             deleted_failed = await _delete_with_retry(
#                 session,
#                 lambda: delete(Task).where(and_(
#                     Task.status == "failed",
#                     Task.created_at < cutoff_failed
#                 )).execution_options(synchronize_session=False)
#             )
            
#             # 3. Пометить зависшие задачи (>1 час) как failed
#             cutoff_stuck = now - timedelta(hours=1)
#             try:
#                 result_stuck = await session.execute(
#                     update(Task)
#                     .where(and_(
#                         Task.status.in_(["queued", "processing"]),
#                         Task.created_at < cutoff_stuck
#                     ))
#                     .values(status="failed")
#                     .execution_options(synchronize_session=False)
#                 )
#                 await session.commit()
#                 marked_failed = result_stuck.rowcount
#             except OperationalError:
#                 await session.rollback()
#                 marked_failed = 0
#                 log.warning("⚠️ Could not mark stuck tasks (deadlock)")
            
#             # 4. Удалить pending платежи старше 24 часов
#             cutoff_pending = now - timedelta(hours=24)
#             deleted_pending = await _delete_with_retry(
#                 session,
#                 lambda: delete(Payment).where(and_(
#                     Payment.status == "pending",
#                     Payment.created_at < cutoff_pending
#                 )).execution_options(synchronize_session=False)
#             )
            
#             # 5. Удалить старые completed/cancelled платежи (30 дней)
#             cutoff_old_payments = now - timedelta(days=30)
#             deleted_old_payments = await _delete_with_retry(
#                 session,
#                 lambda: delete(Payment).where(and_(
#                     Payment.status.in_(["succeeded", "canceled"]),
#                     Payment.created_at < cutoff_old_payments
#                 )).execution_options(synchronize_session=False)
#             )
            
#             log.info(
#                 f"✅ DB Cleanup: "
#                 f"Tasks(completed:{deleted_completed}, failed:{deleted_failed}, stuck:{marked_failed}), "
#                 f"Payments(pending:{deleted_pending}, old:{deleted_old_payments})"
#             )
            
#             # Оптимизация таблиц если удалено много
#             total_deleted = deleted_completed + deleted_failed + deleted_pending + deleted_old_payments
#             if total_deleted > 100:
#                 try:
#                     # Используем text() для raw SQL
#                     await session.execute(text("OPTIMIZE TABLE tasks"))
#                     await session.execute(text("OPTIMIZE TABLE payments"))
#                     await session.commit()
#                     log.info("✅ Tables optimized")
#                 except Exception as e:
#                     log.warning(f"Table optimization skipped: {e}")
            
#             # Статистика
#             try:
#                 tasks_total = await session.scalar(select(func.count(Task.id)))
#                 payments_total = await session.scalar(select(func.count(Payment.id)))
#                 log.info(f"📊 DB Stats: Tasks={tasks_total}, Payments={payments_total}")
#             except Exception:
#                 pass
    
#     except Exception as e:
#         log.error(f"❌ DB cleanup error: {e}", exc_info=True)

"""
✅ Агрессивная очистка БД через ARQ cron с защитой от deadlock
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
    ✅ АГРЕССИВНАЯ очистка БД - держим только свежие записи
    """
    log.info("🧹 Starting AGGRESSIVE database cleanup...")
    
    try:
        async with SessionLocal() as session:
            now = datetime.utcnow()
            
            # ================================
            # 🔥 ИЗМЕНЕНО: Более агрессивная очистка
            # ================================
            
            # 1️⃣ Удалить completed задачи старше 24 ЧАСОВ (было 7 дней)
            cutoff_completed = now - timedelta(hours=24)
            deleted_completed = await _delete_with_retry(
                session,
                lambda: delete(Task).where(and_(
                    Task.status == "completed",
                    Task.created_at < cutoff_completed
                )).execution_options(synchronize_session=False)
            )
            
            # 2️⃣ Удалить failed задачи старше 6 ЧАСОВ (было 3 дня)
            cutoff_failed = now - timedelta(hours=6)
            deleted_failed = await _delete_with_retry(
                session,
                lambda: delete(Task).where(and_(
                    Task.status == "failed",
                    Task.created_at < cutoff_failed
                )).execution_options(synchronize_session=False)
            )
            
            # 3️⃣ Пометить зависшие задачи (>1 час) как failed
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
            
            # 4️⃣ Удалить pending платежи старше 24 часов
            cutoff_pending = now - timedelta(hours=24)
            deleted_pending = await _delete_with_retry(
                session,
                lambda: delete(Payment).where(and_(
                    Payment.status == "pending",
                    Payment.created_at < cutoff_pending
                )).execution_options(synchronize_session=False)
            )
            
            # 5️⃣ Удалить старые completed/cancelled платежи (30 дней)
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
            
            # ================================
            # 🔥 НОВОЕ: Экстренная очистка если таблица >100K записей
            # ================================
            try:
                tasks_total = await session.scalar(select(func.count(Task.id)))
                
                if tasks_total > 100000:
                    log.warning(f"🚨 Tasks table too large: {tasks_total} rows - emergency cleanup!")
                    
                    # Удалить ВСЕ completed старше 1 ЧАСА
                    emergency_cutoff = now - timedelta(hours=1)
                    emergency_deleted = await _delete_with_retry(
                        session,
                        lambda: delete(Task).where(and_(
                            Task.status == "completed",
                            Task.created_at < emergency_cutoff
                        )).execution_options(synchronize_session=False)
                    )
                    
                    log.warning(f"🔥 Emergency cleanup: deleted {emergency_deleted} tasks")
                    
                    # Уведомить админа
                    if ctx and "bot" in ctx:
                        from core.config import settings
                        if settings.ADMIN_ID:
                            try:
                                await ctx["bot"].send_message(
                                    settings.ADMIN_ID,
                                    f"🚨 <b>Emergency DB Cleanup</b>\n\n"
                                    f"Tasks table had <b>{tasks_total}</b> rows\n"
                                    f"Deleted <b>{emergency_deleted}</b> old tasks\n\n"
                                    f"Current: <b>{tasks_total - emergency_deleted}</b> rows",
                                    parse_mode="HTML"
                                )
                            except Exception as e:
                                log.error(f"Failed to send admin alert: {e}")
            except Exception as e:
                log.error(f"Emergency cleanup error: {e}")
            
            # Оптимизация таблиц если удалено много
            total_deleted = deleted_completed + deleted_failed + deleted_pending + deleted_old_payments
            if total_deleted > 1000:  # ✅ ИЗМЕНЕНО: оптимизация только при >1000 удалений
                try:
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


# ================================
# 🔥 НОВОЕ: Одноразовый скрипт для массовой очистки
# ================================

async def emergency_cleanup_now():
    """
    ✅ Экстренная очистка для запуска вручную
    Удаляет ВСЕ старые задачи одним махом
    """
    log.info("🚨 Starting EMERGENCY mass cleanup...")
    
    try:
        async with SessionLocal() as session:
            now = datetime.utcnow()
            
            # Удалить completed старше 1 часа
            cutoff_1h = now - timedelta(hours=1)
            deleted_1h = await _delete_with_retry(
                session,
                lambda: delete(Task).where(and_(
                    Task.status == "completed",
                    Task.created_at < cutoff_1h
                )).execution_options(synchronize_session=False)
            )
            
            # Удалить failed старше 1 часа
            deleted_failed = await _delete_with_retry(
                session,
                lambda: delete(Task).where(and_(
                    Task.status == "failed",
                    Task.created_at < cutoff_1h
                )).execution_options(synchronize_session=False)
            )
            
            log.info(f"✅ Emergency cleanup: deleted {deleted_1h + deleted_failed} tasks")
            
            # Оптимизация
            try:
                await session.execute(text("OPTIMIZE TABLE tasks"))
                await session.commit()
                log.info("✅ Table optimized")
            except Exception as e:
                log.warning(f"Optimization failed: {e}")
            
            # Финальная статистика
            tasks_total = await session.scalar(select(func.count(Task.id)))
            log.info(f"📊 After cleanup: Tasks={tasks_total}")
    
    except Exception as e:
        log.error(f"❌ Emergency cleanup error: {e}", exc_info=True)


if __name__ == "__main__":
    """
    Запуск одноразовой очистки вручную:
    docker-compose exec app python -c "import asyncio; from services.cleanup_db import emergency_cleanup_now; asyncio.run(emergency_cleanup_now())"
    """
    import asyncio
    asyncio.run(emergency_cleanup_now())