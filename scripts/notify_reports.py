import base64
import hashlib
import hmac
import os
import shutil
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib import parse
from zoneinfo import ZoneInfo

import requests

REPORT_ROOT = Path("collected-reports")
OUTPUT_DIR = Path("notification-output")

DINGTALK_SAFE_MAX_BYTES = 19000


def find_reports():
    """Find all TradingAgents complete reports."""
    return sorted(
        REPORT_ROOT.rglob("complete_report.md")
    )


def find_metadata(report_path: Path):
    metadata_path = report_path.parent / "metadata.json"

    if metadata_path.exists():
        try:
            import json

            return json.loads(
                metadata_path.read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            pass

    return {}


def find_decision(report_path: Path):
    decision_path = report_path.parent / "decision.txt"

    if not decision_path.exists():
        return ""

    return decision_path.read_text(
        encoding="utf-8"
    ).strip()


def extract_ticker(report_path: Path):
    metadata = find_metadata(report_path)

    ticker = metadata.get("ticker")

    if ticker:
        return ticker

    # Expected:
    # .../<ticker>/<date>/complete_report.md
    try:
        return report_path.parent.parent.name
    except Exception:
        return report_path.parent.name


def normalize_decision(text: str):
    """
    Keep DingTalk summary reasonably short.

    TradingAgents decision may be either:
    BUY / HOLD / SELL
    or a longer structured explanation.
    """
    if not text:
        return "未获取最终决策"

    text = text.strip()

    # Avoid extremely large cards.
    if len(text) > 1200:
        text = text[:1200] + "\n\n> 完整内容请查看 GitHub Artifact / Email。"

    return text


def build_summary(reports):
    now = datetime.now(
        ZoneInfo("America/New_York")
    )

    title = (
        f"TradingAgents 美股分析报告 "
        f"{now.strftime('%Y-%m-%d')}"
    )

    sections = [
        f"# 📈 {title}",
        "",
        f"> 分析完成时间：{now.strftime('%Y-%m-%d %H:%M:%S')} ET",
        f"> 分析标的：{len(reports)}",
        "",
        "---",
        "",
    ]

    successful = []

    for report in reports:
        ticker = extract_ticker(report)

        decision = normalize_decision(
            find_decision(report)
        )

        metadata = find_metadata(report)

        status = metadata.get(
            "status",
            "unknown",
        )

        successful.append(ticker)

        sections.extend(
            [
                f"## 📊 {ticker}",
                "",
                f"**状态：** {status}",
                "",
                "### 最终决策",
                "",
                decision,
                "",
                "---",
                "",
            ]
        )

    sections.extend(
        [
            "## ℹ️ 说明",
            "",
            "- 完整 Analyst / Research / Trader / Risk / Portfolio 报告已保存至 GitHub Artifact。",
            "- Gmail 邮件附带全部 Markdown 报告 ZIP。",
            "- 本报告仅用于研究，不构成投资建议。",
            "",
        ]
    )

    return title, "\n".join(sections)


def split_utf8_bytes(text, max_bytes):
    """
    Split text while respecting UTF-8 byte boundaries.

    Prefer paragraph boundaries when possible.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    paragraphs = text.split("\n\n")

    chunks = []
    current = ""

    for paragraph in paragraphs:
        candidate = (
            paragraph
            if not current
            else current + "\n\n" + paragraph
        )

        if len(
            candidate.encode("utf-8")
        ) <= max_bytes:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        # A single paragraph itself can still be huge.
        while len(
            paragraph.encode("utf-8")
        ) > max_bytes:

            low = 1
            high = len(paragraph)

            best = 1

            while low <= high:
                mid = (low + high) // 2

                size = len(
                    paragraph[:mid].encode(
                        "utf-8"
                    )
                )

                if size <= max_bytes:
                    best = mid
                    low = mid + 1
                else:
                    high = mid - 1

            chunks.append(
                paragraph[:best]
            )

            paragraph = paragraph[best:]

        current = paragraph

    if current:
        chunks.append(current)

    return chunks


def build_dingtalk_url(
    webhook_url,
    secret,
):
    if not secret:
        return webhook_url

    timestamp = str(
        round(time.time() * 1000)
    )

    string_to_sign = (
        f"{timestamp}\n{secret}"
    )

    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    sign = parse.quote_plus(
        base64.b64encode(hmac_code)
    )

    separator = (
        "&"
        if "?" in webhook_url
        else "?"
    )

    return (
        f"{webhook_url}"
        f"{separator}"
        f"timestamp={timestamp}"
        f"&sign={sign}"
    )


def send_dingtalk(
    title,
    markdown,
):
    webhook_url = os.getenv(
        "DINGTALK_WEBHOOK_URL",
        "",
    ).strip()

    secret = os.getenv(
        "DINGTALK_SECRET",
        "",
    ).strip()

    if not webhook_url:
        print(
            "DINGTALK_WEBHOOK_URL is not configured; skipping DingTalk."
        )
        return True

    chunks = split_utf8_bytes(
        markdown,
        DINGTALK_SAFE_MAX_BYTES,
    )

    print(
        f"DingTalk chunks: {len(chunks)}"
    )

    success = True

    for index, chunk in enumerate(chunks):
        part_title = title

        if len(chunks) > 1:
            part_title = (
                f"{title} "
                f"({index + 1}/{len(chunks)})"
            )

        # Sign each request independently.
        url = build_dingtalk_url(
            webhook_url,
            secret,
        )

        text = chunk

        if index == 0:
            text = (
                f"### {title}\n\n"
                f"{chunk}"
            )

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": part_title[:100],
                "text": text,
            },
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers={
                    "Content-Type":
                        "application/json",
                },
                timeout=30,
            )

            response.raise_for_status()

            result = response.json()

            if result.get(
                "errcode"
            ) != 0:

                print(
                    "DingTalk API error:",
                    result,
                )

                success = False

            else:
                print(
                    f"DingTalk chunk "
                    f"{index + 1} sent."
                )

        except Exception as exc:
            print(
                f"DingTalk send failed: "
                f"{exc}"
            )

            success = False

        if (
            index
            < len(chunks) - 1
        ):
            time.sleep(0.5)

    return success


def create_zip():
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    archive_name = (
        OUTPUT_DIR
        / "TradingAgents-reports"
    )

    path = shutil.make_archive(
        str(archive_name),
        "zip",
        REPORT_ROOT,
    )

    return Path(path)


def markdown_to_basic_html(markdown):
    """
    Minimal HTML wrapper.

    We intentionally do not add a Markdown dependency.
    Gmail also receives the plain-text version.
    """
    import html

    escaped = html.escape(markdown)

    return f"""
    <html>
      <body style="
        font-family:
          -apple-system,
          BlinkMacSystemFont,
          Segoe UI,
          Arial,
          sans-serif;
        line-height: 1.6;
      ">
        <pre style="
          white-space: pre-wrap;
          word-wrap: break-word;
          font-family: inherit;
        ">{escaped}</pre>
      </body>
    </html>
    """


def send_gmail(
    subject,
    body,
    zip_path,
):
    sender = os.getenv(
        "GMAIL_SENDER",
        "",
    ).strip()

    password = os.getenv(
        "GMAIL_APP_PASSWORD",
        "",
    ).replace(" ", "").strip()

    receiver = os.getenv(
        "GMAIL_RECEIVER",
        "",
    ).strip()

    if not (
        sender
        and password
        and receiver
    ):
        print(
            "Gmail secrets are incomplete; skipping email."
        )
        return True

    recipients = [
        item.strip()
        for item
        in receiver.split(",")
        if item.strip()
    ]

    msg = EmailMessage()

    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(
        recipients
    )

    msg.set_content(body)

    msg.add_alternative(
        markdown_to_basic_html(body),
        subtype="html",
    )

    if zip_path.exists():
        msg.add_attachment(
            zip_path.read_bytes(),
            maintype="application",
            subtype="zip",
            filename=zip_path.name,
        )

    try:
        with smtplib.SMTP(
            "smtp.gmail.com",
            587,
            timeout=30,
        ) as server:

            server.ehlo()
            server.starttls()
            server.ehlo()

            server.login(
                sender,
                password,
            )

            server.send_message(
                msg
            )

        print(
            "Gmail notification sent."
        )

        return True

    except smtplib.SMTPAuthenticationError:
        print(
            "Gmail authentication failed. "
            "Check GMAIL_SENDER and GMAIL_APP_PASSWORD."
        )

        return False

    except Exception as exc:
        print(
            f"Gmail send failed: {exc}"
        )

        return False


def main():
    reports = find_reports()

    if not reports:
        raise RuntimeError(
            "No complete_report.md files found "
            "under collected-reports/"
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    title, summary = build_summary(
        reports
    )

    summary_path = (
        OUTPUT_DIR
        / "daily_summary.md"
    )

    summary_path.write_text(
        summary,
        encoding="utf-8",
    )

    zip_path = create_zip()

    print(
        f"Found {len(reports)} reports."
    )

    dingtalk_ok = send_dingtalk(
        title,
        summary,
    )

    gmail_ok = send_gmail(
        title,
        summary,
        zip_path,
    )

    if not dingtalk_ok:
        print(
            "::warning::DingTalk notification failed."
    )

    if not gmail_ok:
        print(
            "::warning::Gmail notification failed."
    )

if __name__ == "__main__":
    main()
