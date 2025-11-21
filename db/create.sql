-- nanoBanana schema bootstrap (MySQL 5.7)
SET NAMES utf8mb4;
SET time_zone = '+00:00';
SET sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION';
SET FOREIGN_KEY_CHECKS = 0;
START TRANSACTION;

CREATE TABLE IF NOT EXISTS `users` (
  `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `chat_id`          BIGINT          NOT NULL,
  `username`         VARCHAR(64)     NULL,
  `email`            VARCHAR(255)    NULL,
  `receipt_opt_out`  TINYINT(1)      NOT NULL DEFAULT 0,
  `balance_credits`  INT UNSIGNED    NOT NULL DEFAULT 0,
  `model_preference` VARCHAR(50)     NOT NULL DEFAULT 'standard',
  `created_at`       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ux_users_chat_id` (`chat_id`),
  KEY `idx_users_username` (`username`),
  KEY `idx_users_email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `payments` (
  `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`          BIGINT UNSIGNED NOT NULL,
  `rub_amount`       DECIMAL(10,2)   NOT NULL,
  `amount`           INT UNSIGNED    NOT NULL,
  `currency`         CHAR(3)         NOT NULL DEFAULT 'RUB',
  `status`           ENUM('pending','awaiting_capture','succeeded','canceled','refunded','failed')
                    NOT NULL DEFAULT 'pending',
  `ext_payment_id`   VARCHAR(64)     NULL,
  `confirmation_url` VARCHAR(512)    NULL,
  `receipt_needed`   TINYINT(1)      NOT NULL DEFAULT 1,
  `receipt_email`    VARCHAR(255)    NULL,
  `created_at`       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ux_payments_ext_id` (`ext_payment_id`),
  KEY `idx_payments_user_status` (`user_id`,`status`),
  CONSTRAINT `fk_payments_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `credit_ledger` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`         BIGINT UNSIGNED NOT NULL,
  `direction`       ENUM('in','out') NOT NULL,
  `source`          ENUM('payment','generation','admin','refund') NOT NULL,
  `amount_credits`  INT UNSIGNED NOT NULL,
  `balance_after`   INT UNSIGNED NOT NULL,
  `ref_type`        ENUM('payment','task','manual') NOT NULL DEFAULT 'manual',
  `ref_id`          BIGINT UNSIGNED NULL,
  `note`            VARCHAR(255) NULL,
  `created_at`      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ledger_user_created` (`user_id`,`created_at`),
  KEY `idx_ledger_ref` (`ref_type`,`ref_id`),
  CONSTRAINT `fk_ledger_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `tasks` (
  `id`                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`            BIGINT UNSIGNED NOT NULL,
  `task_uuid`          CHAR(36)        NOT NULL,
  `prompt`             TEXT            NOT NULL,
  `images`             JSON            NULL,
  `status`             ENUM('queued','processing','completed','failed','moderation_blocked')
                      NOT NULL DEFAULT 'queued',
  `result_text`        MEDIUMTEXT      NULL,
  `result_image_urls`  JSON            NULL,
  `credits_used`       INT UNSIGNED    NULL,
  `error_message`      TEXT            NULL,
  `created_at`         TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`         TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `delivered`          TINYINT(1)      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ux_tasks_task_uuid` (`task_uuid`),
  KEY `idx_tasks_user_created` (`user_id`,`created_at`),
  KEY `idx_tasks_status` (`status`),
  KEY `idx_tasks_delivered` (`delivered`),
  CONSTRAINT `fk_tasks_user` FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `broadcast_jobs` (
  `id`              VARCHAR(36)     NOT NULL,
  `created_at`      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `created_by`      BIGINT          NOT NULL,
  `text`            TEXT            NOT NULL,
  `status`          VARCHAR(20)     NOT NULL DEFAULT 'queued',
  `total`           INT             NOT NULL DEFAULT 0,
  `sent`            INT             NOT NULL DEFAULT 0,
  `failed`          INT             NOT NULL DEFAULT 0,
  `fallback`        INT             NOT NULL DEFAULT 0,
  `note`            TEXT            NULL,
  `media_type`      VARCHAR(20)     NULL,
  `media_file_id`   TEXT            NULL,
  `media_file_path` TEXT            NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

COMMIT;
SET FOREIGN_KEY_CHECKS = 1;