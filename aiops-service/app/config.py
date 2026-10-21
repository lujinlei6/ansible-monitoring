from pydantic_settings import BaseSettings, SettingsConfigDict
'''
它是项目的配置管理模块，通过 Pydantic Settings 从 .env 文件或环境变量读取配置，并根据类型自动进行转换和校验。
代码中集中定义了 Prometheus、DeepSeek、MySQL、Kubernetes Namespace、日志以及预测目标等配置，
同时通过 property 将逗号分隔的字符串转换成 Python 列表，最后通过 settings = Settings() 创建全局配置对象，供项目其他模块使用。
'''

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    prometheus_url: str = "http://192.168.1.40:9090"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout: int = 90
    llm_enabled: bool = True
    max_concurrent_llm: int = 3

    mysql_host: str = "mysql.kube-mall.svc.cluster.local"
    mysql_port: int = 3306
    mysql_db: str = "aiops"
    mysql_user: str = "aiops"
    mysql_password: str = ""

    allowed_namespaces: str = "kube-mall,ingress-nginx,aiops-drill"
    log_tail_lines: int = 120
    max_log_chars: int = 6000
    dedup_window_minutes: int = 30

    predict_targets: str = "worker-1:9100,worker-2:9100,worker-3:9100"

    # AI 增强邮件（Alertmanager 发原始告警，这里补发"根因+修复方案"）
    smtp_enabled: bool = False
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    smtp_from: str = ""
    smtp_password: str = ""
    smtp_to: str = ""

    @property
    def namespaces(self) -> list[str]:
        return [x.strip() for x in self.allowed_namespaces.split(",") if x.strip()]

    @property
    def targets(self) -> list[str]:
        return [x.strip() for x in self.predict_targets.split(",") if x.strip()]


settings = Settings()
