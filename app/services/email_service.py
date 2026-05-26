import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailService:

    @staticmethod
    def _send_with_config(
        to: str,
        subject: str,
        html_body: str,
        host: str,
        port: int,
        user: str,
        password: str,
        from_addr: str,
        tls: bool,
    ) -> None:
        """底层同步发送，接受显式 SMTP 配置，失败时抛出异常（供测试和生产共用）"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if tls:
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=15)

        try:
            if user:
                server.login(user, password)
            server.sendmail(from_addr, [to], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        logger.info(f"✅ 邮件已发送至 {to}，主题：{subject}")

    @staticmethod
    async def _get_smtp_settings() -> dict:
        """读取 SMTP 配置：优先站点配置（缓存 → DB），回退到环境变量"""
        try:
            from app.infrastructure.cache import cache_get, cache_set, TTL_SITE_CONFIG
            cfg = await cache_get("site_config")
            if not cfg:
                from app.models.site_config import SiteConfig
                rows = await SiteConfig.all().values("key", "value")
                if rows:
                    cfg = {r["key"]: r["value"] for r in rows}
                    await cache_set("site_config", cfg, TTL_SITE_CONFIG)
            if cfg and cfg.get("smtp_host"):
                return {
                    "host":      str(cfg["smtp_host"]),
                    "port":      int(cfg.get("smtp_port", 587)),
                    "user":      str(cfg.get("smtp_user", "")),
                    "password":  str(cfg.get("smtp_password", "")),
                    "from_addr": str(cfg.get("smtp_from", "") or cfg.get("smtp_user", "")),
                    "tls":       bool(cfg.get("smtp_tls", True)),
                }
        except Exception:
            pass
        return {
            "host":      settings.SMTP_HOST,
            "port":      settings.SMTP_PORT,
            "user":      settings.SMTP_USER,
            "password":  settings.SMTP_PASSWORD,
            "from_addr": settings.SMTP_FROM,
            "tls":       settings.SMTP_TLS,
        }

    @staticmethod
    async def _send(to: str, subject: str, html_body: str) -> bool:
        """读取站点配置后发送，失败返回 False 并记录日志"""
        try:
            smtp = await EmailService._get_smtp_settings()
            await asyncio.to_thread(EmailService._send_with_config, to, subject, html_body, **smtp)
            return True
        except Exception as e:
            logger.error(f"❌ 邮件发送失败: {e}")
            return False

    @staticmethod
    async def send_password_reset(email: str, username: str, token: str) -> bool:
        """发送密码重置邮件"""
        reset_url = f"{settings.FRONTEND_URL}/auth/reset-password?token={token}"
        html = f"""
        <div style="font-family: sans-serif; max-width: 480px; margin: auto; padding: 32px;">
          <h2 style="color: #0096fa;">Moetopia 密码重置</h2>
          <p>你好，<strong>{username}</strong>，</p>
          <p>我们收到了你的密码重置申请。请点击下方按钮完成重置（链接 <strong>1 小时</strong>内有效）：</p>
          <a href="{reset_url}"
             style="display:inline-block;padding:12px 28px;background:#0096fa;color:#fff;
                    border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0;">
            重置密码
          </a>
          <p style="color:#666;font-size:13px;">如果你没有发起此请求，请忽略这封邮件，你的账号是安全的。</p>
          <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
          <p style="color:#999;font-size:12px;">Moetopia &copy; 2025</p>
        </div>
        """
        return await EmailService._send(email, "Moetopia 密码重置", html)

    @staticmethod
    async def send_welcome(email: str, username: str) -> bool:
        """发送注册欢迎邮件"""
        html = f"""
        <div style="font-family: sans-serif; max-width: 480px; margin: auto; padding: 32px;">
          <h2 style="color: #0096fa;">欢迎加入 Moetopia！</h2>
          <p>你好，<strong>{username}</strong>！</p>
          <p>你的账号已成功创建，快来探索各种精彩作品吧 🎨</p>
          <a href="{settings.FRONTEND_URL}"
             style="display:inline-block;padding:12px 28px;background:#0096fa;color:#fff;
                    border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0;">
            前往 Moetopia
          </a>
          <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
          <p style="color:#999;font-size:12px;">Moetopia &copy; 2025</p>
        </div>
        """
        return await EmailService._send(email, "欢迎加入 Moetopia！", html)


    @staticmethod
    async def send_verification_code(email: str, code: str, purpose: str) -> bool:
        """发送邮箱验证码"""
        purpose_labels = {
            "registration": "注册验证",
            "password_change": "修改密码验证",
            "login_id_change": "修改登录ID验证",
            "account_claim": "账号认领验证",
        }
        label = purpose_labels.get(purpose, "身份验证")
        html = f"""
        <div style="font-family: sans-serif; max-width: 480px; margin: auto; padding: 32px;">
          <h2 style="color: #FF7FAB;">Moetopia {label}</h2>
          <p>你好！</p>
          <p>你的验证码为：</p>
          <div style="font-size: 36px; font-weight: bold; letter-spacing: 8px; color: #FF7FAB;
                      background: #FFF0F5; padding: 16px 24px; border-radius: 12px;
                      text-align: center; margin: 16px 0;">
            {code}
          </div>
          <p style="color: #666;">验证码 <strong>10 分钟</strong>内有效，请勿泄露给他人。</p>
          <p style="color: #666;font-size:13px;">如果你没有发起此请求，请忽略这封邮件。</p>
          <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
          <p style="color:#999;font-size:12px;">Moetopia &copy; 2025</p>
        </div>
        """
        return await EmailService._send(email, f"Moetopia {label}验证码：{code}", html)


    @staticmethod
    async def send_notification_digest(email: str, username: str, notifications: list) -> bool:
        """发送通知摘要邮件（批量整合多条通知）"""
        type_labels = {
            "like": "点赞",
            "comment": "评论",
            "follow": "关注",
            "new_artwork": "新作品",
            "series_update": "系列更新",
            "commission": "约稿",
            "system": "系统通知",
        }

        rows = ""
        for n in notifications[:50]:  # 最多展示 50 条
            label = type_labels.get(n.get("type", ""), n.get("type", ""))
            actor = f"<strong>{n['actor_username']}</strong>" if n.get("actor_username") else "系统"
            rows += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#444;">
                <span style="display:inline-block;padding:2px 8px;background:#FFF0F5;color:#FF7FAB;
                             border-radius:20px;font-size:11px;font-weight:bold;margin-right:8px;">{label}</span>
                {actor} — {n.get("content", "")}
              </td>
            </tr>"""

        count = len(notifications)
        summary = f"共 {count} 条新通知" if count > 1 else "1 条新通知"
        html = f"""
        <div style="font-family:sans-serif;max-width:540px;margin:auto;padding:32px;">
          <h2 style="color:#FF7FAB;margin-bottom:4px;">Moetopia 通知摘要</h2>
          <p style="color:#666;font-size:13px;">你好，<strong>{username}</strong>！你有 <strong>{summary}</strong>：</p>
          <table style="width:100%;border-collapse:collapse;margin:16px 0;background:#fff;
                        border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);">
            {rows}
          </table>
          <a href="{settings.FRONTEND_URL}/notifications"
             style="display:inline-block;padding:10px 24px;background:#FF7FAB;color:#fff;
                    border-radius:8px;text-decoration:none;font-weight:bold;">
            查看全部通知
          </a>
          <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
          <p style="color:#999;font-size:12px;">Moetopia &copy; 2025 &mdash; 你可以在设置页面关闭邮件通知。</p>
        </div>
        """
        return await EmailService._send(email, f"Moetopia 通知摘要（{summary}）", html)


email_service = EmailService()
