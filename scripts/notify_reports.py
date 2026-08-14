import base64
import hashlib
import hmac
import json
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

# 钉钉 Markdown 单条消息预留一定 JSON/标题空间
DINGTALK_SAFE_MAX_BYTES = 19000


def read_json(path: Path) -> dict:
    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def collect_results() -> list[dict]:
    """
    收集每个 ticker 的运行结果。

    成功任务通常包含：
      metadata.json
      decision.txt
      complete_report.md

    失败任务通常包含：
      metadata.json
      error.txt
    """
    results = []

    for metadata_path in sorted(
        REPORT_ROOT.rglob("metadata.json")
    ):
        metadata = read_json(metadata_path)

        run_dir = metadata_path.parent

        ticker = str(
            metadata.get("ticker")
            or run_dir.parent.name
        ).upper()

        analysis_date = str(
            metadata.get("analysis_date")
            or run_dir.name
        )

        status = str(
            metadata.get("status")
            or "unknown"
        )

        decision_path = (
            run_dir / "decision.txt"
        )

        error_path = (
            run_dir / "error.txt"
        )

        report_path = (
            run_dir / "complete_report.md"
        )

        decision = ""

        if decision_path.exists():
            decision = decision_path.read_text(
                encoding="utf-8"
            ).strip()

        error = ""

        if error_path.exists():
            error = error_path.read_text(
                encoding="utf-8"
            ).strip()

        results.append(
            {
                "ticker": ticker,
                "analysis_date": analysis_date,
                "status": status,
                "decision": decision,
                "error": error,
                "report_path": (
                    str(report_path)
                    if report_path.exists()
                    else ""
                ),
            }
        )

    return results


def short_text(
    text: str,
    max_chars: int = 1400,
) -> str:
    text = text.strip()

    if not text:
        return "未获取结果"

    if len(text) <= max_chars:
        return text

    return (
        text[:max_chars]
        + "\n\n"
        + "> 内容已截断，完整报告请查看 Gmail 附件或 GitHub Artifact。"
    )


def build_summary(
    results: list[dict],
) -> tuple[str, str]:
    now = datetime.now(
        ZoneInfo(
            "America/New_York"
        )
    )

    analysis_dates = sorted(
        {
            item["analysis_date"]
            for item in results
            if item["analysis_date"]
        }
    )

    if len(analysis_dates) == 1:
        analysis_date = analysis_dates[0]
    else:
        analysis_date = now.strftime(
            "%Y-%m-%d"
        )

    success_count = sum(
        1
        for item in results
        if item["status"] == "success"
    )

    failed_count = (
        len(results)
        - success_count
    )

    title = (
        f"TradingAgents 美股分析报告 "
        f"{analysis_date}"
    )

    lines = [
        f"# 📈 {title}",
        "",
        (
            f"> 推送时间："
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} ET"
        ),
        (
            f"> 标的数量："
            f"{len(results)}"
        ),
        (
            f"> 成功：{success_count}"
            f"｜失败：{failed_count}"
        ),
        "",
        "---",
        "",
    ]

    for item in results:
        ticker = item["ticker"]
        status = item["status"]

        status_icon = (
            "✅"
            if status == "success"
            else "❌"
        )

        lines.extend(
            [
                f"## {status_icon} {ticker}",
                "",
                f"**状态：** {status}",
                "",
            ]
        )

        if status == "success":
            lines.extend(
                [
                    "### 最终决策",
                    "",
                    short_text(
                        item["decision"]
                    ),
                    "",
                ]
            )

        else:
            error_tail = "\n".join(
                item["error"].splitlines()[
                    -12:
                ]
            )

            lines.extend(
                [
                    "### 失败信息",
                    "",
                    (
                        "```text\n"
                        + short_text(
                            error_tail,
                            1800,
                        )
                        + "\n```"
                    ),
                    "",
                ]
            )

        lines.extend(
            [
                "---",
                "",
            ]
        )

    lines.extend(
        [
            "## ℹ️ 说明",
            "",
            (
                "- 完整 Markdown 报告及失败日志"
                "保存在 GitHub Artifact。"
            ),
            (
                "- Gmail 邮件附带本次全部 "
                "Artifact 内容的 ZIP。"
            ),
            (
                "- 本报告仅用于研究，"
                "不构成投资建议。"
            ),
            "",
        ]
    )

    return (
        title,
        "\n".join(lines),
    )


def split_utf8_bytes(
    text: str,
    max_bytes: int,
) -> list[str]:
    """
    按 UTF-8 字节数切分钉钉 Markdown。

    优先在段落边界切分。
    """
    if (
        len(text.encode("utf-8"))
        <= max_bytes
    ):
        return [text]

    paragraphs = text.split(
        "\n\n"
    )

    chunks: list[str] = []

    current = ""

    for paragraph in paragraphs:
        if not current:
            candidate = paragraph
        else:
            candidate = (
                f"{current}\n\n"
                f"{paragraph}"
            )

        if (
            len(
                candidate.encode(
                    "utf-8"
                )
            )
            <= max_bytes
        ):
            current = candidate
            continue

        if current:
            chunks.append(
                current
            )

            current = ""

        remaining = paragraph

        while (
            len(
                remaining.encode(
                    "utf-8"
                )
            )
            > max_bytes
        ):
            low = 1

            high = len(
                remaining
            )

            best = 1

            while low <= high:
                mid = (
                    low + high
                ) // 2

                current_size = len(
                    remaining[
                        :mid
                    ].encode(
                        "utf-8"
                    )
                )

                if (
                    current_size
                    <= max_bytes
                ):
                    best = mid

                    low = (
                        mid + 1
                    )

                else:
                    high = (
                        mid - 1
                    )

            chunks.append(
                remaining[
                    :best
                ]
            )

            remaining = (
                remaining[
                    best:
                ]
            )

        current = remaining

    if current:
        chunks.append(
            current
        )

    return chunks


def build_dingtalk_url(
    webhook_url: str,
    secret: str,
) -> str:
    """
    钉钉机器人加签：

    timestamp + "\\n" + secret
    -> HMAC-SHA256
    -> Base64
    -> URL encode
    """
    if not secret:
        return webhook_url

    timestamp = str(
        round(
            time.time()
            * 1000
        )
    )

    string_to_sign = (
        f"{timestamp}\n"
        f"{secret}"
    )

    hmac_code = hmac.new(
        secret.encode(
            "utf-8"
        ),
        string_to_sign.encode(
            "utf-8"
        ),
        digestmod=hashlib.sha256,
    ).digest()

    sign = parse.quote_plus(
        base64.b64encode(
            hmac_code
        )
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
    title: str,
    markdown: str,
) -> bool:
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
            "DINGTALK_WEBHOOK_URL "
            "is not configured; "
            "skipping DingTalk."
        )

        return True

    chunks = split_utf8_bytes(
        markdown,
        DINGTALK_SAFE_MAX_BYTES,
    )

    all_success = True

    print(
        f"DingTalk message chunks: "
        f"{len(chunks)}"
    )

    for index, chunk in enumerate(
        chunks
    ):
        display_title = title

        if len(chunks) > 1:
            display_title = (
                f"{title} "
                f"({index + 1}/"
                f"{len(chunks)})"
            )

        if index == 0:
            text = (
                f"### {title}"
                f"\n\n"
                f"{chunk}"
            )
        else:
            text = chunk

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": (
                    display_title[
                        :100
                    ]
                ),
                "text": text,
            },
        }

        try:
            response = requests.post(
                build_dingtalk_url(
                    webhook_url,
                    secret,
                ),
                json=payload,
                headers={
                    "Content-Type":
                        "application/json"
                },
                timeout=30,
            )

            response.raise_for_status()

            result = (
                response.json()
            )

            if (
                result.get(
                    "errcode"
                )
                == 0
            ):
                print(
                    "DingTalk chunk "
                    f"{index + 1}/"
                    f"{len(chunks)} "
                    "sent."
                )

            else:
                print(
                    "DingTalk API error: "
                    f"{result}"
                )

                all_success = False

        except Exception as exc:
            print(
                "DingTalk send failed: "
                f"{exc}"
            )

            all_success = False

        if (
            index
            < len(chunks) - 1
        ):
            time.sleep(
                0.5
            )

    return all_success


def create_zip() -> Path:
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    archive_base = (
        OUTPUT_DIR
        / "TradingAgents-reports"
    )

    archive_path = (
        shutil.make_archive(
            str(
                archive_base
            ),
            "zip",
            REPORT_ROOT,
        )
    )

    return Path(
        archive_path
    )


def markdown_to_basic_html(
    markdown: str,
) -> str:
    """
    不增加额外 Markdown Python 依赖。

    Gmail HTML 版以 pre-wrap 形式显示，
    同时邮件中仍保留纯文本版本。
    """
    import html

    escaped = html.escape(
        markdown
    )

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
    subject: str,
    body: str,
    zip_path: Path,
) -> bool:
    sender = os.getenv(
        "GMAIL_SENDER",
        "",
    ).strip()

    password = (
        os.getenv(
            "GMAIL_APP_PASSWORD",
            "",
        )
        .replace(
            " ",
            "",
        )
        .strip()
    )

    receiver = os.getenv(
        "GMAIL_RECEIVER",
        "",
    ).strip()

    if (
        not sender
        or not password
        or not receiver
    ):
        print(
            "Gmail secrets "
            "are incomplete; "
            "skipping Gmail."
        )

        return True

    recipients = [
        item.strip()
        for item
        in receiver.split(",")
        if item.strip()
    ]

    message = EmailMessage()

    message[
        "Subject"
    ] = subject

    message[
        "From"
    ] = sender

    message[
        "To"
    ] = ", ".join(
        recipients
    )

    message.set_content(
        body
    )

    message.add_alternative(
        markdown_to_basic_html(
            body
        ),
        subtype="html",
    )

    if zip_path.exists():
        message.add_attachment(
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
                message
            )

        print(
            "Gmail notification "
            "sent to: "
            + ", ".join(
                recipients
            )
        )

        return True

    except smtplib.SMTPAuthenticationError:
        print(
            "Gmail authentication "
            "failed. Check "
            "GMAIL_SENDER and "
            "GMAIL_APP_PASSWORD."
        )

        return False

    except Exception as exc:
        print(
            "Gmail send failed: "
            f"{exc}"
        )

        return False


def main() -> None:
    results = (
        collect_results()
    )

    if not results:
        raise RuntimeError(
            "No metadata.json files "
            "found under "
            "collected-reports/."
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    title, summary = (
        build_summary(
            results
        )
    )

    summary_path = (
        OUTPUT_DIR
        / "daily_summary.md"
    )

    summary_path.write_text(
        summary,
        encoding="utf-8",
    )

    zip_path = (
        create_zip()
    )

    print(
        f"Collected "
        f"{len(results)} "
        "ticker result(s)."
    )

    dingtalk_ok = (
        send_dingtalk(
            title,
            summary,
        )
    )

    gmail_ok = (
        send_gmail(
            title,
            summary,
            zip_path,
        )
    )

    # 通知失败只产生 Warning，
    # 不改变分析结果的成功状态。
    if not dingtalk_ok:
        print(
            "::warning::"
            "DingTalk notification "
            "failed."
        )

    if not gmail_ok:
        print(
            "::warning::"
            "Gmail notification "
            "failed."
        )


if __name__ == "__main__":
    main()
