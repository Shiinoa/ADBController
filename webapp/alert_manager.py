"""
Alert Manager - SMTP Email and Interchat Integration
"""
import smtplib
import ssl
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
import urllib3
import json
from datetime import datetime
from typing import List, Dict, Optional
import asyncio

from database import get_setting, get_all_settings, log_alert

DEFAULT_SMTP_TIMEOUT_SECONDS = 60


class AlertManager:
    def __init__(self):
        self.settings: Dict[str, str] = {}
        self.reload_settings()

    def reload_settings(self):
        """Reload settings from database"""
        self.settings = get_all_settings()

    def _get_interchat_setting(self, key: str, legacy_key: str = "") -> str:
        """Read Interchat settings with fallback to legacy Synology keys."""
        value = self.settings.get(key, "")
        if value:
            return value
        return self.settings.get(legacy_key, "") if legacy_key else ""

    def _deliver_email(
        self,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        to_emails: Optional[List[str]] = None,
    ) -> Dict:
        """Build and deliver an email using the configured SMTP settings."""
        self.reload_settings()

        smtp_host = self.settings.get('smtp_host', '')
        smtp_port = int(self.settings.get('smtp_port', 587))
        smtp_user = self.settings.get('smtp_user', '')
        smtp_password = self.settings.get('smtp_password', '')
        smtp_from = self.settings.get('smtp_from', '')
        smtp_security = self.settings.get('smtp_security', 'starttls')  # none, ssl, starttls
        smtp_auth_enabled = self.settings.get('smtp_auth_enabled', 'true') == 'true'
        raw_timeout = (self.settings.get('smtp_timeout_seconds', '') or '').strip()
        try:
            smtp_timeout = max(5, int(raw_timeout)) if raw_timeout else DEFAULT_SMTP_TIMEOUT_SECONDS
        except ValueError:
            smtp_timeout = DEFAULT_SMTP_TIMEOUT_SECONDS

        if not smtp_host or not smtp_from:
            message = "SMTP host or from address not configured"
            print(f"[Alert] {message}")
            return {"success": False, "message": message}

        if smtp_auth_enabled and not all([smtp_user, smtp_password]):
            message = "SMTP auth enabled but credentials not configured"
            print(f"[Alert] {message}")
            return {"success": False, "message": message}

        if not to_emails:
            to_emails = [e.strip() for e in self.settings.get('smtp_to', '').split(',') if e.strip()]

        if not to_emails:
            message = "No recipient emails configured"
            print(f"[Alert] {message}")
            return {"success": False, "message": message}

        try:
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = smtp_from
            message["To"] = ", ".join(to_emails)

            message.attach(MIMEText(body, "plain"))
            if html_body:
                message.attach(MIMEText(html_body, "html"))

            context = ssl.create_default_context()
            stage = "connect"

            if smtp_security == 'ssl':
                with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=smtp_timeout) as server:
                    stage = "login" if smtp_auth_enabled else "sendmail"
                    if smtp_auth_enabled:
                        server.login(smtp_user, smtp_password)
                    stage = "sendmail"
                    server.sendmail(smtp_from, to_emails, message.as_string())
            elif smtp_security == 'starttls':
                with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout) as server:
                    stage = "starttls"
                    server.starttls(context=context)
                    stage = "login" if smtp_auth_enabled else "sendmail"
                    if smtp_auth_enabled:
                        server.login(smtp_user, smtp_password)
                    stage = "sendmail"
                    server.sendmail(smtp_from, to_emails, message.as_string())
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout) as server:
                    stage = "login" if smtp_auth_enabled else "sendmail"
                    if smtp_auth_enabled:
                        server.login(smtp_user, smtp_password)
                    stage = "sendmail"
                    server.sendmail(smtp_from, to_emails, message.as_string())

            print(f"[Alert] Email sent to {to_emails}")
            log_alert("", "email", subject, ", ".join(to_emails))
            return {
                "success": True,
                "message": f"Email sent to {', '.join(to_emails)}",
                "recipients": to_emails,
            }
        except socket.timeout:
            error_message = f"SMTP {stage} timed out after {smtp_timeout} seconds"
            print(f"[Alert] Email error: {error_message}")
            return {"success": False, "message": error_message}
        except smtplib.SMTPServerDisconnected as exc:
            error_message = f"SMTP {stage} failed: {exc}"
            print(f"[Alert] Email error: {error_message}")
            return {"success": False, "message": error_message}
        except Exception as exc:
            error_message = f"SMTP {stage} failed: {exc}"
            print(f"[Alert] Email error: {error_message}")
            return {"success": False, "message": error_message}

    def send_email(self, subject: str, body: str, html_body: Optional[str] = None, to_emails: Optional[List[str]] = None) -> bool:
        """Send email via SMTP"""
        return self._deliver_email(subject, body, html_body, to_emails).get("success", False)

    def send_email_result(
        self,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        to_emails: Optional[List[str]] = None,
    ) -> Dict:
        """Send email via SMTP and return a detailed result."""
        return self._deliver_email(subject, body, html_body, to_emails)

    def _post_interchat(self, message: str, attachments: Optional[List[Dict]] = None) -> Dict:
        """Post an Interchat webhook request and return detailed result."""
        self.reload_settings()

        url = self._get_interchat_setting('interchat_url', 'syno_chat_url')
        token = self._get_interchat_setting('interchat_token', 'syno_chat_token')
        username = self._get_interchat_setting('interchat_username') or "ADB Control Center"
        icon_url = self._get_interchat_setting('interchat_icon_url')
        skip_ssl_verification = self._get_interchat_setting('interchat_skip_ssl_verification').lower() == 'true'

        if not url:
            message = "Interchat URL not configured"
            print(f"[Alert] {message}")
            return {"success": False, "message": message}

        try:
            payload = {
                "text": message,
                "username": username,
                "icon_url": icon_url,
                "attachments": attachments or [],
            }

            # Add token if provided
            if token:
                if '?' in url:
                    url = f"{url}&token={token}"
                else:
                    url = f"{url}?token={token}"

            if skip_ssl_verification:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
                verify=not skip_ssl_verification,
            )

            if response.ok:
                print(f"[Alert] Interchat message sent")
                log_alert("", "interchat", message, url)
                return {
                    "success": True,
                    "message": "Interchat message sent",
                    "status_code": response.status_code,
                    "payload": payload,
                }
            else:
                error_message = f"HTTP {response.status_code}: {response.text[:500]}"
                print(f"[Alert] Interchat error: {error_message}")
                return {
                    "success": False,
                    "message": error_message,
                    "status_code": response.status_code,
                    "payload": payload,
                }

        except Exception as e:
            error_message = str(e)
            print(f"[Alert] Interchat error: {error_message}")
            return {"success": False, "message": error_message}

    def send_interchat(self, message: str, attachments: Optional[List[Dict]] = None) -> bool:
        """Send message to Interchat webhook."""
        return self._post_interchat(message, attachments).get("success", False)

    def send_syno_chat(self, message: str) -> bool:
        """Backward-compatible wrapper for legacy automation flows."""
        return self.send_interchat(message)

    def send_offline_alert(self, devices: List[Dict]):
        """Send alert for offline devices"""
        if not devices:
            return

        self.reload_settings()

        if self.settings.get('alert_enabled', 'false') != 'true':
            print("[Alert] Alerts disabled")
            return

        # Build message
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        device_list = "\n".join([f"- {d.get('ip', 'Unknown')} ({d.get('name', 'Unknown')})" for d in devices])

        # Plain text message
        text_message = f"""TV Monitor Alert - Devices Offline

Time: {now}
Offline Devices ({len(devices)}):
{device_list}

Please check these devices.
"""

        # HTML message
        html_message = f"""
<html>
<body style="font-family: Arial, sans-serif;">
    <h2 style="color: #e74c3c;">TV Monitor Alert - Devices Offline</h2>
    <p><strong>Time:</strong> {now}</p>
    <p><strong>Offline Devices ({len(devices)}):</strong></p>
    <ul>
        {"".join([f'<li style="color: #e74c3c;">{d.get("ip", "Unknown")} - {d.get("name", "Unknown")}</li>' for d in devices])}
    </ul>
    <p>Please check these devices.</p>
    <hr>
    <p style="color: #666; font-size: 12px;">ADB Control Center</p>
</body>
</html>
"""

        # Interchat message
        syno_message = f"🔴 *TV Monitor Alert*\n\nTime: {now}\nOffline Devices ({len(devices)}):\n{device_list}"

        # Send alerts
        self.send_email(f"[ALERT] {len(devices)} TV Devices Offline", text_message, html_message)
        self.send_interchat(syno_message)

    def send_daily_report(self, report_data: Dict, template: Optional[Dict] = None):
        """Send daily status report with optional template"""
        return self.send_daily_report_result(report_data, template).get("success", False)

    def send_daily_report_result(self, report_data: Dict, template: Optional[Dict] = None) -> Dict:
        """Send daily status report with optional template and return a detailed result."""
        self.reload_settings()

        now = datetime.now().strftime("%Y-%m-%d")
        total = report_data.get('total', 0)
        online = report_data.get('online', 0)
        offline = report_data.get('offline', 0)
        devices = report_data.get('devices', [])
        uptime = round((online / total * 100)) if total > 0 else 0

        # Use template if provided, otherwise use default
        if template and template.get('elements'):
            html_message = self._render_template_email(template, report_data, now)
        else:
            html_message = self._render_default_email(report_data, now)

        # Plain text
        text_message = f"""TV Monitor Daily Report - {now}

Summary:
- Total Devices: {total}
- Online: {online}
- Offline: {offline}
- Uptime: {uptime}%

Generated by ADB Control Center
"""

        subject = f"[Daily Report] TV Monitor Status - {now}"
        result = self.send_email_result(subject, text_message, html_message)
        if result.get("success"):
            result["template_name"] = template.get('name', 'Default') if template else 'Default'
        return result

    def _render_default_email(self, report_data: Dict, date: str) -> str:
        """Render default email template"""
        total = report_data.get('total', 0)
        online = report_data.get('online', 0)
        offline = report_data.get('offline', 0)
        devices = report_data.get('devices', [])

        # Build device table
        device_rows = ""
        for d in devices:
            status_color = "#27ae60" if d.get('status') == 'online' else "#e74c3c"
            status_text = "Online" if d.get('status') == 'online' else "Offline"
            app_status = d.get('appStatus', 'Unknown')
            app_color = "#27ae60" if app_status == 'RUNNING' else ("#f39c12" if app_status == 'STOPPED' else "#95a5a6")
            device_rows += f"""
            <tr>
                <td style="padding: 8px; border: 1px solid #ddd;">{d.get('ip', '')}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{d.get('name', '')}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{d.get('location', '')}</td>
                <td style="padding: 8px; border: 1px solid #ddd; color: {status_color};">{status_text}</td>
                <td style="padding: 8px; border: 1px solid #ddd; color: {app_color};">{app_status}</td>
            </tr>
            """

        return f"""
<html>
<body style="font-family: Arial, sans-serif;">
    <h2>TV Monitor Daily Report - {date}</h2>

    <h3>Summary</h3>
    <table style="border-collapse: collapse; margin-bottom: 20px;">
        <tr>
            <td style="padding: 10px; background: #3498db; color: white;">Total Devices</td>
            <td style="padding: 10px; font-size: 24px; font-weight: bold;">{total}</td>
        </tr>
        <tr>
            <td style="padding: 10px; background: #27ae60; color: white;">Online</td>
            <td style="padding: 10px; font-size: 24px; font-weight: bold; color: #27ae60;">{online}</td>
        </tr>
        <tr>
            <td style="padding: 10px; background: #e74c3c; color: white;">Offline</td>
            <td style="padding: 10px; font-size: 24px; font-weight: bold; color: #e74c3c;">{offline}</td>
        </tr>
    </table>

    <h3>Device Status</h3>
    <table style="border-collapse: collapse; width: 100%;">
        <thead>
            <tr style="background: #34495e; color: white;">
                <th style="padding: 10px; border: 1px solid #ddd;">IP Address</th>
                <th style="padding: 10px; border: 1px solid #ddd;">Device Name</th>
                <th style="padding: 10px; border: 1px solid #ddd;">Location</th>
                <th style="padding: 10px; border: 1px solid #ddd;">Status</th>
                <th style="padding: 10px; border: 1px solid #ddd;">App Status</th>
            </tr>
        </thead>
        <tbody>
            {device_rows}
        </tbody>
    </table>

    <hr>
    <p style="color: #666; font-size: 12px;">Generated by ADB Control Center</p>
</body>
</html>
"""

    def _render_template_email(self, template: Dict, report_data: Dict, date: str) -> str:
        """Render email using custom template"""
        total = report_data.get('total', 0)
        online = report_data.get('online', 0)
        offline = report_data.get('offline', 0)
        devices = report_data.get('devices', [])
        uptime = round((online / total * 100)) if total > 0 else 0
        now = datetime.now()

        html = '<html><body style="font-family: Arial, sans-serif; padding: 20px;">'

        for el in template.get('elements', []):
            el_type = el.get('type') if isinstance(el, dict) else el

            if el_type == 'header':
                html += f'''
                <div style="text-align: center; margin-bottom: 24px;">
                    <h1 style="color: #1f2937; margin: 0;">TV Device Status Report</h1>
                    <p style="color: #6b7280; margin: 8px 0 0 0;">Daily Monitoring Report</p>
                </div>'''

            elif el_type == 'company-info':
                html += f'''
                <div style="margin-bottom: 16px; padding: 16px; background: #f3f4f6; border-radius: 8px;">
                    <table style="width: 100%; font-size: 14px;">
                        <tr>
                            <td style="padding: 4px 0;"><strong>Company:</strong> ADB Control Center</td>
                            <td style="padding: 4px 0;"><strong>Department:</strong> IT Operations</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 0;"><strong>Prepared by:</strong> System Admin</td>
                            <td style="padding: 4px 0;"><strong>Generated:</strong> {now.strftime("%Y-%m-%d %H:%M:%S")}</td>
                        </tr>
                    </table>
                </div>'''

            elif el_type == 'date-info':
                html += f'''
                <div style="margin-bottom: 16px; padding: 12px; background: #eef2ff; border-left: 4px solid #6366f1; border-radius: 4px;">
                    <table style="width: 100%;">
                        <tr>
                            <td style="font-weight: 500; color: #374151;">Report Date:</td>
                            <td style="text-align: right; font-size: 18px; font-weight: bold; color: #6366f1;">{date}</td>
                        </tr>
                    </table>
                </div>'''

            elif el_type == 'stats-summary':
                html += f'''
                <table style="width: 100%; margin-bottom: 24px; border-collapse: separate; border-spacing: 8px;">
                    <tr>
                        <td style="padding: 16px; background: #dbeafe; text-align: center; border-radius: 8px;">
                            <p style="font-size: 28px; font-weight: bold; color: #2563eb; margin: 0;">{total}</p>
                            <p style="font-size: 12px; color: #4b5563; margin: 4px 0 0 0;">Total Devices</p>
                        </td>
                        <td style="padding: 16px; background: #dcfce7; text-align: center; border-radius: 8px;">
                            <p style="font-size: 28px; font-weight: bold; color: #16a34a; margin: 0;">{online}</p>
                            <p style="font-size: 12px; color: #4b5563; margin: 4px 0 0 0;">Online</p>
                        </td>
                        <td style="padding: 16px; background: #fee2e2; text-align: center; border-radius: 8px;">
                            <p style="font-size: 28px; font-weight: bold; color: #dc2626; margin: 0;">{offline}</p>
                            <p style="font-size: 12px; color: #4b5563; margin: 4px 0 0 0;">Offline</p>
                        </td>
                        <td style="padding: 16px; background: #f3e8ff; text-align: center; border-radius: 8px;">
                            <p style="font-size: 28px; font-weight: bold; color: #9333ea; margin: 0;">{uptime}%</p>
                            <p style="font-size: 12px; color: #4b5563; margin: 4px 0 0 0;">Uptime</p>
                        </td>
                    </tr>
                </table>'''

            elif el_type == 'device-table':
                device_rows = ""
                for i, d in enumerate(devices):
                    bg_color = "#ffffff" if i % 2 == 0 else "#f9fafb"
                    status_color = "#16a34a" if d.get('status') == 'online' else "#dc2626"
                    status_text = "Online" if d.get('status') == 'online' else "Offline"
                    app_status = d.get('appStatus', 'Unknown')
                    app_color = "#16a34a" if app_status == 'RUNNING' else ("#f59e0b" if app_status == 'STOPPED' else "#6b7280")
                    device_rows += f'''
                    <tr style="background: {bg_color};">
                        <td style="padding: 8px; border: 1px solid #e5e7eb;">{i + 1}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb; color: {status_color};">● {status_text}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb; font-family: monospace;">{d.get('ip', '')}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb;">{d.get('name', '')}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb;">{d.get('location', '')}</td>
                        <td style="padding: 8px; border: 1px solid #e5e7eb; color: {app_color};">{app_status}</td>
                    </tr>'''

                html += f'''
                <div style="margin-bottom: 24px;">
                    <h3 style="color: #374151; margin-bottom: 12px;">All Devices</h3>
                    <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
                        <thead>
                            <tr style="background: #374151; color: white;">
                                <th style="padding: 10px; border: 1px solid #4b5563;">#</th>
                                <th style="padding: 10px; border: 1px solid #4b5563;">Status</th>
                                <th style="padding: 10px; border: 1px solid #4b5563;">IP Address</th>
                                <th style="padding: 10px; border: 1px solid #4b5563;">Device Name</th>
                                <th style="padding: 10px; border: 1px solid #4b5563;">Location</th>
                                <th style="padding: 10px; border: 1px solid #4b5563;">App Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            {device_rows}
                        </tbody>
                    </table>
                </div>'''

            elif el_type == 'offline-table':
                offline_devices = [d for d in devices if d.get('status') != 'online']
                if offline_devices:
                    offline_rows = ""
                    for i, d in enumerate(offline_devices):
                        offline_rows += f'''
                        <tr style="background: #ffffff;">
                            <td style="padding: 8px; border: 1px solid #fca5a5;">{i + 1}</td>
                            <td style="padding: 8px; border: 1px solid #fca5a5; font-family: monospace;">{d.get('ip', '')}</td>
                            <td style="padding: 8px; border: 1px solid #fca5a5;">{d.get('name', '')}</td>
                            <td style="padding: 8px; border: 1px solid #fca5a5;">{d.get('location', '')}</td>
                            <td style="padding: 8px; border: 1px solid #fca5a5;">{d.get('workCenter', '')}</td>
                        </tr>'''

                    html += f'''
                    <div style="margin-bottom: 24px;">
                        <h3 style="color: #dc2626; margin-bottom: 12px;">Offline Devices ({len(offline_devices)})</h3>
                        <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
                            <thead>
                                <tr style="background: #fee2e2;">
                                    <th style="padding: 10px; border: 1px solid #fca5a5;">#</th>
                                    <th style="padding: 10px; border: 1px solid #fca5a5;">IP Address</th>
                                    <th style="padding: 10px; border: 1px solid #fca5a5;">Device Name</th>
                                    <th style="padding: 10px; border: 1px solid #fca5a5;">Location</th>
                                    <th style="padding: 10px; border: 1px solid #fca5a5;">Work Center</th>
                                </tr>
                            </thead>
                            <tbody>
                                {offline_rows}
                            </tbody>
                        </table>
                    </div>'''
                else:
                    html += '''
                    <div style="margin-bottom: 24px;">
                        <h3 style="color: #dc2626; margin-bottom: 12px;">Offline Devices (0)</h3>
                        <p style="padding: 12px; background: #dcfce7; color: #16a34a; border-radius: 4px;">All devices are online!</p>
                    </div>'''

            elif el_type == 'notes':
                html += '''
                <div style="margin-bottom: 24px;">
                    <h3 style="color: #374151; margin-bottom: 8px;">Notes</h3>
                    <div style="padding: 16px; border: 1px solid #e5e7eb; border-radius: 4px; background: #f9fafb; min-height: 60px;">
                        <p style="color: #6b7280; font-size: 14px; font-style: italic; margin: 0;">No additional notes</p>
                    </div>
                </div>'''

            elif el_type == 'signature':
                html += '''
                <table style="width: 100%; margin-top: 32px;">
                    <tr>
                        <td style="text-align: center; width: 50%; padding: 0 20px;">
                            <div style="border-top: 1px solid #9ca3af; padding-top: 8px; margin-top: 48px;">
                                <p style="font-weight: 500; margin: 0;">Prepared By</p>
                                <p style="font-size: 12px; color: #6b7280; margin: 4px 0 0 0;">Date: _______________</p>
                            </div>
                        </td>
                        <td style="text-align: center; width: 50%; padding: 0 20px;">
                            <div style="border-top: 1px solid #9ca3af; padding-top: 8px; margin-top: 48px;">
                                <p style="font-weight: 500; margin: 0;">Approved By</p>
                                <p style="font-size: 12px; color: #6b7280; margin: 4px 0 0 0;">Date: _______________</p>
                            </div>
                        </td>
                    </tr>
                </table>'''

            elif el_type == 'divider':
                html += '<hr style="margin: 16px 0; border: none; border-top: 1px solid #e5e7eb;">'

        html += '''
        <hr style="margin-top: 24px; border: none; border-top: 1px solid #e5e7eb;">
        <p style="color: #9ca3af; font-size: 12px; text-align: center; margin-top: 16px;">Generated by ADB Control Center</p>
        </body></html>'''

        return html

    async def test_smtp(self) -> Dict:
        """Send a real test email using the configured SMTP settings."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"[SMTP Test] ADB Control Center - {now}"
        text_body = (
            "This is a test email from ADB Control Center.\n\n"
            f"Sent at: {now}\n"
            "If you received this message, SMTP delivery is working."
        )
        html_body = f"""
<html>
<body style="font-family: Arial, sans-serif;">
    <h2>SMTP Test Email</h2>
    <p>This is a test email from <strong>ADB Control Center</strong>.</p>
    <p><strong>Sent at:</strong> {now}</p>
    <p>If you received this message, SMTP delivery is working.</p>
</body>
</html>
"""
        result = self._deliver_email(subject, text_body, html_body)
        if result.get("success"):
            return {
                "success": True,
                "message": result.get("message", "Test email sent"),
                "recipients": result.get("recipients", []),
            }
        return result

    async def test_interchat(self) -> Dict:
        """Test Interchat connection"""
        result = self._post_interchat("🔔 Test message from ADB Control Center")
        if result.get("success"):
            return {"success": True, "message": "Interchat test message sent"}
        return result

    async def test_syno_chat(self) -> Dict:
        """Backward-compatible alias for Interchat test."""
        return await self.test_interchat()


# Global instance
alert_manager = AlertManager()
