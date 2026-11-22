-- 封存观测更正：重算只影响未派单热区，已派单热区的成员或锚点受保护。
-- 旧数据卷幂等升级：先按 information_schema 判断，再扩展楼层小时状态枚举。
SET @schema_name := DATABASE();
SET @enum_ready := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'floor_hours' AND COLUMN_NAME = 'status'
      AND COLUMN_TYPE LIKE '%recomputing%'
);
SET @ddl := IF(@enum_ready = 0,
    "ALTER TABLE floor_hours MODIFY status ENUM('collecting','queued','ready','failed','recomputing') NOT NULL DEFAULT 'collecting'",
    'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS observation_corrections (
    id INT PRIMARY KEY AUTO_INCREMENT,
    hour_id INT NOT NULL,
    store_id INT NOT NULL,
    window_start DATETIME NOT NULL,
    old_count SMALLINT NOT NULL,
    new_count SMALLINT NOT NULL CHECK (new_count BETWEEN 0 AND 300),
    reason VARCHAR(120) NOT NULL,
    status ENUM('pending','rejected','applied') NOT NULL DEFAULT 'pending',
    result_note VARCHAR(200) NULL,
    submitted_by INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    -- 重试复用同一行，不新增更正记录。
    UNIQUE (hour_id, store_id),
    INDEX (hour_id, status),
    CHECK ((status = 'pending' AND result_note IS NULL) OR
           (status <> 'pending' AND result_note IS NOT NULL)),
    CHECK (old_count <> new_count),
    FOREIGN KEY (hour_id) REFERENCES floor_hours(id),
    FOREIGN KEY (store_id) REFERENCES stores(id),
    FOREIGN KEY (submitted_by) REFERENCES users(id)
) ENGINE=InnoDB;
