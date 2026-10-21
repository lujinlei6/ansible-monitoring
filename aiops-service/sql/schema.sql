CREATE DATABASE IF NOT EXISTS aiops
  CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS incidents (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  fingerprint   VARCHAR(64)  NOT NULL,
  alertname     VARCHAR(128) NOT NULL,
  severity      VARCHAR(16)  NOT NULL,
  status        VARCHAR(16)  NOT NULL,
  instance      VARCHAR(128),
  node          VARCHAR(64),
  namespace     VARCHAR(64),
  pod           VARCHAR(191),
  labels        JSON,
  annotations   JSON,
  starts_at     DATETIME     NOT NULL,
  ends_at       DATETIME     NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_fp_start (fingerprint, starts_at),
  KEY idx_created (created_at),
  KEY idx_alertname (alertname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS diagnoses (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  incident_id   BIGINT       NOT NULL,
  root_cause    TEXT         NOT NULL,
  risk_level    VARCHAR(8)   NOT NULL,
  confidence    DECIMAL(3,2) NOT NULL DEFAULT 0.50,
  evidence      JSON,
  suggestions   JSON,
  fix_plan      JSON,
  raw_response  MEDIUMTEXT,
  source        VARCHAR(16)  NOT NULL DEFAULT 'llm',
  model         VARCHAR(64),
  latency_ms    INT,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_incident (incident_id),
  KEY idx_created (created_at),
  CONSTRAINT fk_diag_incident FOREIGN KEY (incident_id)
    REFERENCES incidents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS predictions (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  metric          VARCHAR(32)  NOT NULL,
  instance        VARCHAR(128) NOT NULL,
  horizon_hours   INT          NOT NULL,
  target_time     DATETIME     NOT NULL,
  predicted_value DOUBLE       NOT NULL,
  lower_bound     DOUBLE,
  upper_bound     DOUBLE,
  model           VARCHAR(32)  NOT NULL,
  mae             DOUBLE,
  rmse            DOUBLE,
  mape            DOUBLE,
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_metric_instance (metric, instance, target_time),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS feedback (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  diagnosis_id  BIGINT       NOT NULL,
  is_correct    TINYINT(1)   NOT NULL,
  comment       VARCHAR(500),
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_diag (diagnosis_id),
  CONSTRAINT fk_feedback_diag FOREIGN KEY (diagnosis_id)
    REFERENCES diagnoses(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
