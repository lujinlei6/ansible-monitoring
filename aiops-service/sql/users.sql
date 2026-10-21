-- aiops 库读写用户（AI 服务用）与只读用户（Grafana 用）
-- 使用 mysql_native_password，避免 MySQL 8 caching_sha2_password 非 SSL 下的
-- "Public Key Retrieval is not allowed" 报错
CREATE USER IF NOT EXISTS 'aiops'@'%' IDENTIFIED WITH mysql_native_password BY 'AIOps@2026db';
GRANT SELECT, INSERT, UPDATE, DELETE ON aiops.* TO 'aiops'@'%';
CREATE USER IF NOT EXISTS 'aiops_ro'@'%' IDENTIFIED WITH mysql_native_password BY 'AIOps@2026ro';
GRANT SELECT ON aiops.* TO 'aiops_ro'@'%';
FLUSH PRIVILEGES;
