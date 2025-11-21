"""
✅ Автобэкап БД через ARQ cron с улучшенным логированием и уведомлениями
Запускается каждый час в :05 минут
"""
import logging
import subprocess
import re
import gzip
import shutil
import os
import asyncio
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from aiogram import Bot
from aiogram.types import FSInputFile

from core.config import settings

log = logging.getLogger("backup_db")


async def send_backup_to_admin(ctx, backup_file: Path, size_mb: float, size_gz_mb: float, compressed: bool = True):
    """Отправка уведомления о бэкапе админу"""
    bot: Bot = ctx.get("bot")
    if not bot:
        log.warning("⚠️ Bot not available in context")
        return
    
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # ✅ МАЛЫЙ ФАЙЛ - отправляем в Telegram
        if size_gz_mb < 50:
            log.info(f"📤 Sending backup to admin (size: {size_gz_mb:.2f} MB)...")
            
            caption = (
                f"💾 <b>Database Backup</b>\n\n"
                f"📅 {timestamp}\n"
            )
            
            if compressed:
                compression_ratio = ((1 - size_gz_mb/size_mb)*100)
                caption += (
                    f"📊 Original: {size_mb:.2f} MB\n"
                    f"📦 Compressed: {size_gz_mb:.2f} MB\n"
                    f"💾 Compression: {compression_ratio:.1f}%\n\n"
                )
            else:
                caption += f"📊 Size: {size_mb:.2f} MB\n\n"
            
            caption += "✅ Backup completed"
            
            # Retry механизм
            for attempt in range(1, 4):
                try:
                    await bot.send_document(
                        settings.ADMIN_ID,
                        document=FSInputFile(backup_file),
                        caption=caption,
                        parse_mode="HTML",
                        request_timeout=300
                    )
                    log.info("✅ Backup sent to admin successfully")
                    return
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    
                    if "internal" in error_msg and attempt < 3:
                        wait_time = 5 * attempt
                        log.warning(f"⚠️ Telegram error, retry {attempt}/3 in {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    if attempt == 3:
                        log.error(f"❌ Failed to send backup: {e}")
                        raise
        
        # ✅ БОЛЬШОЙ ФАЙЛ - уведомление о локальном хранении
        else:
            log.warning(f"⚠️ Backup too large ({size_gz_mb:.2f} MB) - saving locally")
            
            compression_ratio = ((1 - size_gz_mb/size_mb)*100) if compressed else 0
            
            message = (
                f"💾 <b>Database Backup Created</b>\n\n"
                f"📅 {timestamp}\n"
                f"📊 Original: <b>{size_mb:.2f} MB</b>\n"
                f"📦 Compressed: <b>{size_gz_mb:.2f} MB</b> "
                f"({compression_ratio:.1f}% saved)\n\n"
                f"💡 <b>Status:</b>\n"
                f"✅ Backup saved locally (too large for Telegram)\n"
                f"📁 Location: <code>/tmp/backups/</code>\n"
                f"🔒 Keeping last 24 backups\n\n"
                f"<i>Use SFTP/SCP for download if needed</i>"
            )
            
            await bot.send_message(
                settings.ADMIN_ID,
                message,
                parse_mode="HTML"
            )
            log.info("✅ Large backup notification sent")
    
    except Exception as e:
        log.error(f"❌ Failed to notify admin: {e}", exc_info=True)


async def cleanup_old_backups(backup_dir: Path, keep_count: int = 1):  # ✅ ИЗМЕНЕНО: keep_count=1
    """✅ УЛУЧШЕНО: Удаление старых бэкапов, оставляем только последний"""
    try:
        backups = sorted(
            backup_dir.glob("nanoBanana_*.sql.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        
        total_backups = len(backups)
        
        if total_backups <= keep_count:
            log.info(f"📁 Backups: {total_backups} (keeping all)")
            return None
        
        to_delete = backups[keep_count:]
        deleted_count = 0
        freed_mb = 0
        deleted_names = []
        
        for backup in to_delete:
            try:
                size = backup.stat().st_size / (1024 * 1024)
                backup.unlink()
                deleted_count += 1
                freed_mb += size
                deleted_names.append(backup.name)
            except Exception as e:
                log.warning(f"Failed to delete {backup.name}: {e}")
        
        log.info(f"🗑️ Deleted {deleted_count} old backups (freed {freed_mb:.2f} MB)")
        
        return {
            "deleted_count": deleted_count,
            "freed_mb": freed_mb,
            "kept_count": keep_count,
            "total_before": total_backups,
            "oldest_deleted": deleted_names[0] if deleted_names else None
        }
    
    except Exception as e:
        log.error(f"❌ Cleanup old backups error: {e}")
        return None

async def backup_database_task(ctx):
    """
    ARQ периодическая задача бэкапа
    Вызывается каждый час в :05 минут
    """
    log.info("💾 ========== STARTING DATABASE BACKUP ==========")
    
    backup_dir = Path("/tmp/backups")
    backup_dir.mkdir(exist_ok=True, parents=True)
    log.info(f"💾 Backup directory: {backup_dir}")
    
    # ✅ ПРОВЕРКА 1: mysqldump установлен?
    log.info("🔍 Checking if mysqldump is installed...")
    try:
        result = subprocess.run(
            ["which", "mysqldump"],
            capture_output=True,
            timeout=5
        )
        if result.returncode != 0:
            log.error("❌ mysqldump not installed! Install: apt-get install mysql-client")
            
            # Уведомить админа
            if settings.ADMIN_ID:
                try:
                    bot: Bot = ctx.get("bot")
                    if bot:
                        await bot.send_message(
                            settings.ADMIN_ID,
                            "❌ <b>Backup Error</b>\n\n"
                            "mysqldump not found!\n"
                            "Please install: <code>apt-get install mysql-client</code>",
                            parse_mode="HTML"
                        )
                except Exception as e:
                    log.error(f"Failed to send admin notification: {e}")
            return
        else:
            mysqldump_path = result.stdout.decode().strip()
            log.info(f"✅ mysqldump found at: {mysqldump_path}")
    except Exception as e:
        log.error(f"❌ Cannot check mysqldump: {e}")
        return
    
    # ✅ ПРОВЕРКА 2: Достаточно места на диске?
    log.info("🔍 Checking disk space...")
    try:
        stat = shutil.disk_usage("/app")
        free_gb = stat.free / (1024**3)
        total_gb = stat.total / (1024**3)
        used_gb = stat.used / (1024**3)
        
        log.info(f"💾 Disk: Total={total_gb:.2f}GB, Used={used_gb:.2f}GB, Free={free_gb:.2f}GB")
        
        if free_gb < 1.0:  # Меньше 1 GB свободно
            log.error(f"❌ Low disk space: {free_gb:.2f} GB free")
            
            if settings.ADMIN_ID:
                try:
                    bot: Bot = ctx.get("bot")
                    if bot:
                        await bot.send_message(
                            settings.ADMIN_ID,
                            f"❌ <b>Backup Skipped</b>\n\n"
                            f"Low disk space: {free_gb:.2f} GB\n"
                            f"Need at least 1 GB free",
                            parse_mode="HTML"
                        )
                except Exception as e:
                    log.error(f"Failed to send disk alert: {e}")
            return
        
        log.info(f"✅ Disk space OK: {free_gb:.2f} GB free")
    except Exception as e:
        log.warning(f"⚠️ Cannot check disk space: {e}")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"nanoBanana_{timestamp}.sql"
    backup_file_gz = backup_dir / f"nanoBanana_{timestamp}.sql.gz"
    
    log.info(f"📝 Backup files will be: {backup_file.name} -> {backup_file_gz.name}")
    
    try:
        # Парсим DSN
        log.info("🔍 Parsing DB_DSN...")
        match = re.match(
            r"mysql\+aiomysql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)",
            settings.DB_DSN
        )
        
        if not match:
            log.error(f"❌ Cannot parse DB_DSN: {settings.DB_DSN[:50]}...")
            return
        
        user, password, host, port, database_with_params = match.groups()
        
        # ✅ ИСПРАВЛЕНО: удаляем query параметры из имени БД
        database = database_with_params.split('?')[0]
        
        log.info(f"✅ DB Config: user={user}, host={host}, port={port}, db={database}")
        
        # ✅ Создаем конфиг файл с паролем
        log.info("🔐 Creating MySQL config file...")
        with NamedTemporaryFile(mode='w', suffix='.cnf', delete=False) as cnf_file:
            cnf_file.write(f"[mysqldump]\n")
            cnf_file.write(f"user={user}\n")
            cnf_file.write(f"password={password}\n")
            cnf_file.write(f"host={host}\n")
            cnf_file.write(f"port={port}\n")
            cnf_path = cnf_file.name
        
        try:
            # Защищаем файл паролей
            os.chmod(cnf_path, 0o600)
            log.info(f"✅ Config file created: {cnf_path}")
            
            log.info(f"🔄 Creating backup with mysqldump...")
            
            # ✅ ИСПРАВЛЕНО: флаги для обхода проблем с привилегиями
            cmd = [
                "mysqldump",
                f"--defaults-file={cnf_path}",
                "--single-transaction",
                "--triggers",
                "--quick",
                "--skip-lock-tables",
                "--skip-ssl",
                "--no-tablespaces",  
                database  
            ]
            
            log.info(f"📝 Running: mysqldump --defaults-file=... {database}")
            
            with open(backup_file, "w") as f:
                result = subprocess.run(
                    cmd,
                    stdout=f,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=600  # 10 минут
                )
            
            if result.returncode != 0:
                log.error(f"❌ mysqldump failed: {result.stderr}")
                
                # Удаляем неполный бэкап
                if backup_file.exists():
                    backup_file.unlink()
                
                return
            
            log.info(f"✅ mysqldump completed successfully")
        
        finally:
            # ✅ Удаляем файл с паролем
            try:
                os.unlink(cnf_path)
                log.info(f"🗑️ Config file deleted")
            except Exception:
                pass
        
        # Проверяем размер
        size_mb = backup_file.stat().st_size / (1024 * 1024)
        log.info(f"📊 Backup size: {size_mb:.2f} MB")
        
        # ✅ ПРОВЕРКА: Бэкап не пустой?
        if size_mb < 0.1:  # Меньше 100 KB
            log.error(f"❌ Backup too small ({size_mb:.2f} MB) - probably failed")
            backup_file.unlink()
            return
        
        log.info(f"✅ Backup created: {size_mb:.2f} MB")
        
        # ✅ ПРОВЕРКА: Хватит ли места для сжатия?
        if stat.free < backup_file.stat().st_size * 0.5:
            log.warning(f"⚠️ Not enough space for compression, sending uncompressed")
            
            # Отправить несжатый
            if settings.ADMIN_ID and size_mb < 50:
                await send_backup_to_admin(ctx, backup_file, size_mb, size_mb, compressed=False)
            
            backup_file.unlink()
            return
        
        # Сжимаем
        log.info(f"🔄 Compressing backup...")
        with open(backup_file, 'rb') as f_in:
            with gzip.open(backup_file_gz, 'wb', compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        backup_file.unlink()
        
        size_gz_mb = backup_file_gz.stat().st_size / (1024 * 1024)
        compression_ratio = (1 - size_gz_mb / size_mb) * 100
        log.info(f"✅ Compressed: {size_gz_mb:.2f} MB (saved {compression_ratio:.1f}%)")
        
        # Отправить админу
        if settings.ADMIN_ID:
            log.info(f"📤 Notifying admin (ID: {settings.ADMIN_ID})...")
            await send_backup_to_admin(ctx, backup_file_gz, size_mb, size_gz_mb, compressed=True)
        else:
            log.warning("⚠️ ADMIN_ID not set, cannot send notification")
        
        # ✅ УЛУЧШЕНО: Очистить старые с уведомлением
        cleanup_stats = await cleanup_old_backups(backup_dir, keep_count=1)  # ✅ ИСПРАВЛЕНО
        
        # ✅ ИТОГОВОЕ УВЕДОМЛЕНИЕ С ПОЛНОЙ СТАТИСТИКОЙ
        if settings.ADMIN_ID and cleanup_stats:
            bot: Bot = ctx.get("bot")
            if bot:
                cleanup_msg = (
                    f"🗑️ <b>Cleanup Report</b>\n\n"
                    f"Deleted: <b>{cleanup_stats['deleted_count']}</b> old backups\n"
                    f"Freed: <b>{cleanup_stats['freed_mb']:.1f} MB</b>\n"
                    f"Keeping: <b>{cleanup_stats['kept_count']}</b> latest backups\n"
                )
                
                if cleanup_stats['oldest_deleted']:
                    cleanup_msg += f"\n📅 Oldest deleted:\n<code>{cleanup_stats['oldest_deleted']}</code>"
                
                try:
                    await bot.send_message(
                        settings.ADMIN_ID,
                        cleanup_msg,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    log.warning(f"Failed to send cleanup notification: {e}")
        
        log.info("✅ ========== BACKUP COMPLETED SUCCESSFULLY ==========")
    
    except subprocess.TimeoutExpired:
        log.error("❌ Backup timeout (>10 min)")
        
        # Удалить неполный бэкап
        if backup_file.exists():
            backup_file.unlink()
        
        # ✅ Уведомление об ошибке
        if settings.ADMIN_ID:
            try:
                bot: Bot = ctx.get("bot")
                if bot:
                    await bot.send_message(
                        settings.ADMIN_ID,
                        "❌ <b>Backup Failed</b>\n\n"
                        "Timeout (>10 min)\n"
                        "Check server resources",
                        parse_mode="HTML"
                    )
            except Exception:
                pass
    
    except Exception as e:
        log.error(f"❌ Backup error: {e}", exc_info=True)
        
        # Удалить неполные файлы
        if backup_file.exists():
            backup_file.unlink()
        if backup_file_gz.exists():
            backup_file_gz.unlink()
        
        # ✅ Уведомление об ошибке админу
        if settings.ADMIN_ID:
            try:
                bot: Bot = ctx.get("bot")
                if bot:
                    await bot.send_message(
                        settings.ADMIN_ID,
                        f"❌ <b>Backup Failed</b>\n\n"
                        f"Error: <code>{str(e)[:200]}</code>\n\n"
                        f"Check logs for details",
                        parse_mode="HTML"
                    )
            except Exception as notify_error:
                log.error(f"Failed to send error notification: {notify_error}")