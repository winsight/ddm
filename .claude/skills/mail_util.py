#!/usr/bin/env python3
"""QQ Mail utility — send and check emails via SMTP/IMAP.

Usage:
  python3 mail_util.py send <to> <subject> <body>
  python3 mail_util.py check [count]

Dependencies: Python 3 stdlib only (smtplib + imaplib + email).
"""

from __future__ import annotations

import email
import imaplib
import smtplib
import sys
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formataddr

# ---- QQ Mail config ----
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587
IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
SENDER = "wssssorg@qq.com"

# Auth code stored outside version control
import os as _os
_AUTH_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "..", "mail_config")
try:
    with open(_AUTH_FILE) as _f:
        AUTH_CODE = _f.read().strip()
except FileNotFoundError:
    AUTH_CODE = _os.environ.get("DDM_MAIL_AUTH", "")
    if not AUTH_CODE:
        raise RuntimeError(
            f"Mail auth not found. Create {_AUTH_FILE} with the auth code, "
            f"or set DDM_MAIL_AUTH environment variable.")


def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email via QQ SMTP."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr(("DDM System", SENDER))
    msg["To"] = to
    msg["Subject"] = subject

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        server.starttls()
        server.login(SENDER, AUTH_CODE)
        server.sendmail(SENDER, [to], msg.as_string())
        server.quit()
        print(f"✓ 邮件已发送: {to}")
        print(f"  主题: {subject}")
        return True
    except Exception as e:
        print(f"✗ 发送失败: {e}")
        return False


def _decode_header_str(hdr) -> str:
    """Decode an email header value to string."""
    if hdr is None:
        return ""
    parts = decode_header(hdr)
    result = []
    for text, charset in parts:
        if isinstance(text, bytes):
            result.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(text))
    return "".join(result)


def check_emails(count: int = 5) -> bool:
    """Fetch and display the latest N emails via QQ IMAP."""
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=15)
        mail.login(SENDER, AUTH_CODE)
        mail.select("INBOX")

        status, data = mail.search(None, "ALL")
        if status != "OK":
            print("✗ 搜索邮件失败")
            mail.logout()
            return False

        ids = data[0].split()
        total = len(ids)
        latest = ids[-count:] if len(ids) >= count else ids
        latest.reverse()

        print(f"收件箱共 {total} 封，显示最近 {len(latest)} 封:\n")

        for i, msg_id in enumerate(latest, 1):
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject = _decode_header_str(msg["Subject"])
            sender = _decode_header_str(msg["From"])
            date = msg.get("Date", "")

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="replace")[:200]
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")[:200]

            print(f"━━━ {i}. {subject[:60]} ━━━")
            print(f"  发件人: {sender[:50]}")
            print(f"  时间:   {date}")
            if body.strip():
                print(f"  {body.strip()[:150]}")
            print()

        mail.logout()
        return True
    except Exception as e:
        print(f"✗ 连接失败: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 mail_util.py send <to> <subject> <body>")
        print("  python3 mail_util.py check [count]")
        sys.exit(1)

    action = sys.argv[1]

    if action == "send":
        if len(sys.argv) < 5:
            print("用法: python3 mail_util.py send <to> <subject> <body>")
            sys.exit(1)
        to = sys.argv[2]
        subject = sys.argv[3]
        body = sys.argv[4]
        ok = send_email(to, subject, body)
        sys.exit(0 if ok else 1)

    elif action == "check":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        ok = check_emails(count)
        sys.exit(0 if ok else 1)

    else:
        print(f"未知操作: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
