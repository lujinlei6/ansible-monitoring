"""诊断完成后的「AI 增强邮件」：把根因、证据、修复方案一并发出。

Alertmanager 只能发"原始告警"邮件，且模板里无法回调 AI 接口；
因此由 aiops-service 在诊断完成后主动补发一封，作为告警邮件的增强。
发送失败只记日志，绝不影响诊断主链路。
"""
import html
import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText

from .config import settings

log = logging.getLogger("uvicorn.error")  # 走 uvicorn logger，便于 kubectl logs 看到发信结果


def _li(items) -> str:
    return "".join(f"<li>{html.escape(str(x))}</li>" for x in (items or []))


def _fix_plan_html(plan) -> str:
    if not plan:
        return "<p>（本次未给出可执行修复步骤）</p>"
    rows = []
    for s in plan:
        cmd = s.get("command") or ""
        cmd_html = f"<br><code style='background:#f4f4f5;padding:2px 4px'>{html.escape(cmd)}</code>" if cmd else ""
        rows.append(
            f"<li><b>[{html.escape(str(s.get('risk','')))}] {html.escape(str(s.get('action','')))}</b>{cmd_html}</li>"
        )
    return "<ol>" + "".join(rows) + "</ol>"


def build_email_html(alert: dict, result: dict) -> tuple[str, str]:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    name = labels.get("alertname", "Unknown")
    scope = " / ".join(filter(None, [labels.get("node"), labels.get("namespace"), labels.get("pod"), labels.get("instance")]))
    subject = f"[AIOps诊断][{result.get('risk_level','-')}] {name} @ {labels.get('node') or labels.get('instance') or '-'}"
    body = f"""<div style="font-family:system-ui,-apple-system,'PingFang SC',sans-serif;font-size:14px;color:#171717">
  <h3 style="margin:0 0 8px">AI 诊断报告 · {html.escape(name)}</h3>
  <p style="color:#52525b;margin:0 0 12px">{html.escape(annotations.get('summary',''))}<br>{html.escape(annotations.get('description',''))}</p>
  <table style="border-collapse:collapse;margin-bottom:12px">
    <tr><td style="padding:2px 8px 2px 0;color:#52525b">对象</td><td>{html.escape(scope or '-')}</td></tr>
    <tr><td style="padding:2px 8px 2px 0;color:#52525b">风险等级</td><td><b>{html.escape(str(result.get('risk_level','-')))}</b></td></tr>
    <tr><td style="padding:2px 8px 2px 0;color:#52525b">置信度</td><td>{result.get('confidence','-')}（source={html.escape(str(result.get('source','llm')))}）</td></tr>
  </table>
  <p><b>根因</b><br>{html.escape(str(result.get('root_cause','-')))}</p>
  <p><b>支持证据</b></p><ul>{_li(result.get('evidence'))}</ul>
  <p><b>修复方案（按顺序执行）</b></p>{_fix_plan_html(result.get('fix_plan'))}
  <p><b>排查建议</b></p><ul>{_li(result.get('suggestions'))}</ul>
  <p style="color:#a1a1aa;font-size:12px">由 aiops-service 自动生成；仅为辅助建议，动手前请人工确认。原始告警邮件由 Alertmanager 单独发送。</p>
</div>"""
    return subject, body


def send_diagnosis(alert: dict, result: dict) -> None:
    if not settings.smtp_enabled or not settings.smtp_password:
        return
    try:
        subject, body = build_email_html(alert, result)
        msg = MIMEText(body, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = settings.smtp_from
        msg["To"] = settings.smtp_to
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                s.login(settings.smtp_from, settings.smtp_password)
                s.sendmail(settings.smtp_from, settings.smtp_to.split(","), msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                s.starttls()
                s.login(settings.smtp_from, settings.smtp_password)
                s.sendmail(settings.smtp_from, settings.smtp_to.split(","), msg.as_string())
        log.info("diagnosis mail sent: %s", subject)
    except Exception as exc:  # 邮件失败不影响诊断
        log.warning("send diagnosis mail failed: %s", exc)
